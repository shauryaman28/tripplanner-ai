"""Plan a trip, then ask Prometheus, Jaeger, Loki and Grafana what they saw of it (Phase 29).

    python scripts/observability.py up                       # the stack (docker/observability/)
    python scripts/observability_check.py                    # a backend on http://localhost:8000
    python scripts/observability_check.py --prodlike         # the production-like stack's, on :8200
    python scripts/observability_check.py --api https://…    # any other, read by this machine's Prometheus

The backend must have been told where traces go and to log JSON:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318  LOG_FORMAT=json

What is asked, in the roadmap's words:

    1. the stack answers
    2. the backend shows its metrics — to those it should, and Prometheus is reading them
    3. a trip is planned (a real one: the keys in the backend's settings are used)
    4. "request rate / error rate / duration per endpoint", "custom LLM and cache metrics visible"
    5. "one planning request produces a single connected trace spanning FastAPI → SQLAlchemy →
       Redis → MCP … no orphaned spans"
    6. "searching by trip_id in the logging UI returns log lines from every service touched"
    7. the Grafana dashboard is there, and every panel's query is answered
    8. Phase 34: the four alerts are loaded and evaluated, Prometheus hands them to Alertmanager, the
       receiver answers — and after a healthy plan none of them fires for this backend
    9. Phase 34: GET /admin/health-deep says every dependency works — and answers only the operator

Exit code 0 when every expectation held. `--skip-plan` asks 1, 2, 7, 8 and 9 only. That each alert
fires when it should is scripts/alert_drill.py's to show: it breaks things on purpose.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import secrets
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent

PLAN_TIMEOUT_S = 240.0
SETTLE_S = 60.0  # how long the stores are given to have everything: scrapes are 5 s apart, spans leave every 2 s
UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
BACKEND, TOOL_SERVER = "tripplanner-backend", "tripplanner-mcp"
PLAN_REQUEST = "POST /trips/{trip_id}/plan"


class Checks:
    def __init__(self) -> None:
        self.passed = self.failed = 0

    def expect(self, ok: bool, what: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        print(f"  {'✓' if ok else '✗'} {what}" + (f"  — {detail}" if detail else ""), flush=True)
        return bool(ok)


def get_json(url: str, **kwargs) -> dict:
    answer = httpx.get(url, timeout=20, **kwargs)
    answer.raise_for_status()
    return answer.json()


def until(condition, seconds: float = SETTLE_S, every: float = 2.0):
    """Ask again until the answer is truthy, or time is up. Returns the last answer."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            answer = condition()
        except Exception as exc:
            answer = None
            last_error = exc  # noqa: F841 — kept for a debugger
        if answer or time.monotonic() >= deadline:
            return answer
        time.sleep(every)


# ── 1. The stack ───────────────────────────────────────────────────────────


def the_stack_answers(checks: Checks, where: argparse.Namespace) -> bool:
    print("\n1. The stack answers")

    def answers(url: str) -> tuple[bool, str]:
        try:
            got = httpx.get(url, timeout=5)
            return got.status_code == 200, f"HTTP {got.status_code}"
        except Exception as exc:
            return False, type(exc).__name__

    up = True
    for name, url in (
        ("Prometheus", f"{where.prometheus}/-/ready"),
        ("Jaeger", f"{where.jaeger}/api/v3/services"),
        ("Loki", f"{where.loki}/ready"),
        ("Grafana", f"{where.grafana}/api/health"),
        ("Alertmanager", f"{where.alertmanager}/-/ready"),  # Phase 34
        ("the alert receiver", f"{where.receiver}/alerts"),
    ):
        ok, said = answers(url)
        up &= checks.expect(
            ok, f"{name} at {url.rsplit('/', 2)[0] if name == 'Prometheus' else urlsplit(url).netloc}", said
        )
    if not up:
        print("     start it with: python scripts/observability.py up")
    return up


# ── 2. The backend's metrics ───────────────────────────────────────────────


def the_backend_shows_its_metrics(checks: Checks, where: argparse.Namespace) -> str | None:
    """Returns the `instance` label Prometheus knows this backend by, or None."""
    print(f"\n2. The backend shows its metrics, and Prometheus reads them  ({where.api})")
    key = {"Authorization": f"Bearer {where.ops_token}"} if where.ops_token else {}
    try:
        shown = httpx.get(f"{where.api}/metrics", headers=key, timeout=10)
    except Exception as exc:
        checks.expect(False, "GET /metrics answers", f"{type(exc).__name__}: is the backend running at {where.api}?")
        return None
    checks.expect(
        shown.status_code == 200 and "http_requests_total" in shown.text,
        "GET /metrics answers in Prometheus's format" + (" — to the operations token" if key else ""),
        f"HTTP {shown.status_code}, {len(shown.text.splitlines())} lines",
    )
    if key:
        closed = httpx.get(f"{where.api}/metrics", timeout=10).status_code
        checks.expect(closed == 404, "…and to nobody without it", f"HTTP {closed} without the token")
    checks.expect(
        "/metrics" not in get_json(f"{where.api}/openapi.json").get("paths", {}),
        "it is not one of the API's documented routes",
    )

    port = urlsplit(where.api).port or (443 if where.api.startswith("https") else 80)

    def target():
        for found in get_json(f"{where.prometheus}/api/v1/targets")["data"]["activeTargets"]:
            if urlsplit(found["scrapeUrl"]).port == port and found["health"] == "up":
                return found
        return None

    found = until(target, 30)
    checks.expect(
        found is not None,
        "Prometheus's target for it is up",
        f"job {found['labels']['job']}, read every {found['scrapeInterval']}"
        if found
        else f"no target on port {port} is up — see {where.prometheus}/targets",
    )
    return found["labels"]["instance"] if found else None


# ── 3. A trip ──────────────────────────────────────────────────────────────


def a_trip_is_planned(checks: Checks, api: str) -> dict | None:
    print("\n3. A trip is planned")
    account = {
        "email": f"observability-check-{secrets.token_hex(5)}@example.com",
        "password": secrets.token_urlsafe(16),
    }
    made = httpx.post(f"{api}/auth/register", json=account, timeout=30)
    if not checks.expect(made.status_code == 201, "an account is made", f"HTTP {made.status_code}"):
        return None
    user_id = made.json()["id"]
    login = httpx.post(
        f"{api}/auth/login", data={"username": account["email"], "password": account["password"]}, timeout=30
    )
    token = login.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    start = date.today() + timedelta(days=45)
    trip = httpx.post(
        f"{api}/trips",
        headers=auth,
        timeout=30,
        json={
            "destination": "Jaipur",
            "start_date": str(start),
            "end_date": str(start + timedelta(days=3)),
            "budget": 70_000,
            "group_size": 2,
            "interests": ["history", "food"],
        },
    )
    if not checks.expect(trip.status_code == 201, "a trip is created", f"HTTP {trip.status_code}"):
        return None
    trip_id = trip.json()["id"]

    # Two requests that fail, on purpose: an error rate needs errors to be a rate of.
    httpx.get(f"{api}/trips/{trip_id}", timeout=30)  # no login: 401
    httpx.get(f"{api}/trips/00000000-0000-4000-8000-000000000000", headers=auth, timeout=30)  # nobody's trip: 404

    began = time.monotonic()
    asked = httpx.post(
        f"{api}/trips/{trip_id}/plan",
        headers=auth,
        timeout=60,
        json={"raw_input": "A relaxed long weekend of forts and good food"},
    )
    if not checks.expect(asked.status_code == 202, "POST /plan answers at once", f"HTTP {asked.status_code}"):
        return None
    status: dict = {}
    while time.monotonic() - began < PLAN_TIMEOUT_S:
        status = httpx.get(f"{api}/trips/{trip_id}/status", headers=auth, timeout=30).json()
        if status.get("status") != "planning":
            break
        time.sleep(2)
    took = time.monotonic() - began
    checks.expect(
        status.get("status") == "completed",
        "the plan is made",
        f"{took:.1f} s"
        if status.get("status") == "completed"
        else f"{status.get('status')}: {status.get('failure_reason')}",
    )
    # A rate needs a counter that moved between two of Prometheus's readings: a few requests, a few seconds apart.
    for _ in range(3):
        for asked_for in ("/trips", f"/trips/{trip_id}", f"/trips/{trip_id}/itinerary", f"/trips/{trip_id}/runs"):
            httpx.get(f"{api}{asked_for}", headers=auth, timeout=30)
        time.sleep(3)
    print(f"     trip {trip_id}\n     user {user_id}")
    print(f"     left behind: the account {account['email']} and its one trip")
    return {"trip_id": trip_id, "user_id": user_id, "token": token, "completed": status.get("status") == "completed"}


# ── 4. Metrics ─────────────────────────────────────────────────────────────


def the_metrics_show_it(checks: Checks, prometheus: str, instance: str) -> None:
    print(
        f"\n4. Prometheus: rate, errors and duration per endpoint; the models, the cache, the builds  (instance {instance})"
    )
    here = f'instance="{instance}"'

    def query(expression: str) -> list[dict]:
        return get_json(f"{prometheus}/api/v1/query", params={"query": expression})["data"]["result"]

    def value(expression: str) -> float | None:
        found = query(expression)
        return float(found[0]["value"][1]) if found else None

    # Prometheus has what the run ended with once the run itself is counted.
    counted = 'sum(planning_run_duration_seconds_count{%s, kind="plan"})' % here
    until(lambda: (value(counted) or 0) >= 1, SETTLE_S)

    plan = 'handler="/trips/{trip_id}/plan", method="POST"'
    checks.expect(
        (value(f'sum(http_requests_total{{{here}, {plan}, status="2xx"}})') or 0) >= 1,
        "requests are counted per endpoint, as the router names it",
        'http_requests_total{handler="/trips/{trip_id}/plan", method="POST", status="2xx"}',
    )
    handlers = {found["metric"]["handler"] for found in query(f"sum by (handler) (http_requests_total{{{here}}})")}
    with_an_id = sorted(handler for handler in handlers if UUID.search(handler))
    checks.expect(
        len(handlers) >= 5 and not with_an_id,
        "no endpoint's name has an id in it",
        f"{len(handlers)} endpoints" if not with_an_id else f"{with_an_id[:2]}",
    )
    failed = {
        found["metric"]["handler"]: found["value"][1]
        for found in query(f'sum by (handler) (http_requests_total{{{here}, status="4xx"}})')
    }
    checks.expect(
        float(failed.get("/trips/{trip_id}", 0)) >= 2,
        "failed requests are counted by class — the two that were made to fail",
        f"4xx at /trips/{{trip_id}}: {failed.get('/trips/{trip_id}', 0)}",
    )
    rates = query(f"sum by (handler) (rate(http_requests_total{{{here}}}[5m])) > 0")
    checks.expect(
        len(rates) >= 3,
        "a request rate per endpoint can be asked for",
        f"{len(rates)} endpoints with traffic in the last 5 min",
    )
    p95 = query(
        f"histogram_quantile(0.95, sum by (handler, le) (rate(http_request_duration_seconds_bucket{{{here}}}[5m])))"
    )
    timed = {found["metric"]["handler"]: float(found["value"][1]) for found in p95 if found["value"][1] != "NaN"}
    checks.expect(
        "/trips/{trip_id}/status" in timed,
        "a duration per endpoint can be asked for",
        ", ".join(
            f"{handler} p95 {seconds * 1000:.0f} ms"
            for handler, seconds in sorted(timed.items(), key=lambda t: -t[1])[:3]
        ),
    )

    calls = query(f"sum by (agent, model) (llm_calls_total{{{here}}})")
    checks.expect(
        len(calls) >= 2 and all(found["metric"].get("agent") and found["metric"].get("model") for found in calls),
        "llm_calls_total, by agent and model",
        "; ".join(
            f"{found['metric']['agent']} → {found['metric']['model']}: {float(found['value'][1]):.0f}"
            for found in calls[:5]
        ),
    )
    ratio = value(f"mcp_cache_hit_ratio{{{here}}}")
    checks.expect(
        ratio is not None and 0.0 <= ratio <= 1.0,
        "mcp_cache_hit_ratio is a ratio",
        f"{ratio:.2f} over the last five minutes"
        if ratio is not None and ratio == ratio
        else "no number: no lookup in the last five minutes?",
    )
    lookups = collections.defaultdict(dict)
    for found in query(f"sum by (cache, result) (mcp_cache_lookups_total{{{here}}})"):
        lookups[found["metric"]["cache"]][found["metric"]["result"]] = int(float(found["value"][1]))
    # At least one hit: a trip's caches are warmed as it is created, and planning it finds them. (Not
    # "and a miss": a second trip to the same place within a few minutes is answered from the cache whole.)
    checks.expect(
        any(results.get("hit") for results in lookups.values()) and sum(sum(r.values()) for r in lookups.values()) >= 3,
        "…made of the tool server's lookups, counted in the backend",
        "; ".join(f"{cache}: {results}" for cache, results in sorted(lookups.items())),
    )
    builds = {
        found["metric"]["status"]: float(found["value"][1]) for found in query(f"itinerary_builds_total{{{here}}}")
    }
    checks.expect(
        set(builds) == {"success", "failure", "retry"} and builds.get("success", 0) >= 1,
        "itinerary_builds_total, by status",
        ", ".join(f"{status} {count:.0f}" for status, count in sorted(builds.items())),
    )
    runs = query(f"sum by (kind, outcome) (planning_run_duration_seconds_count{{{here}}})")
    took = value(f'sum(planning_run_duration_seconds_sum{{{here}, kind="plan", outcome="completed"}})')
    checks.expect(
        any(found["metric"] == {"kind": "plan", "outcome": "completed"} for found in runs),
        "the run itself is timed — the request that started it was answered at once",
        f"plan/completed: {took:.1f} s in all" if took else str([found["metric"] for found in runs]),
    )
    tools = {
        found["metric"]["tool"]: float(found["value"][1])
        for found in query(f"sum by (tool) (mcp_tool_calls_total{{{here}}})")
    }
    checks.expect(
        len(tools) >= 3,
        "calls to the tool server are counted by tool",
        ", ".join(f"{tool} {count:.0f}" for tool, count in sorted(tools.items())),
    )


# ── 5. The trace ───────────────────────────────────────────────────────────


def spans_of(jaeger: str, trip_id: str) -> list[dict]:
    """Every span Jaeger has that is tagged with this trip, as flat dicts."""
    now = datetime.now(timezone.utc)
    found = get_json(
        f"{jaeger}/api/v3/traces",
        params={
            "query.service_name": BACKEND,
            "query.attributes": json.dumps({"trip.id": trip_id}),
            "query.start_time_min": (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "query.start_time_max": (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "query.num_traces": 100,
        },
    )
    spans = []
    for resource in found.get("result", {}).get("resourceSpans", []):
        service = next(
            (a["value"].get("stringValue") for a in resource["resource"]["attributes"] if a["key"] == "service.name"),
            "?",
        )
        for scope in resource.get("scopeSpans", []):
            for span in scope.get("spans", []):
                spans.append(
                    {
                        "trace": span["traceId"],
                        "id": span["spanId"],
                        "parent": span.get("parentSpanId") or "",
                        "name": span["name"],
                        "kind": span.get("kind", 0),
                        "service": service,
                        "scope": scope.get("scope", {}).get("name", ""),
                        "ended": int(span.get("endTimeUnixNano") or 0),
                        "began": int(span.get("startTimeUnixNano") or 0),
                        "attributes": {
                            a["key"]: next(iter(a["value"].values()), None) for a in span.get("attributes", [])
                        },
                        "text": json.dumps(span),
                    }
                )
    return spans


def the_plans_trace(jaeger: str, trip_id: str) -> list[dict]:
    """The spans of the trace whose root is the request that asked for the plan; [] when there is none."""
    by_trace = collections.defaultdict(list)
    for span in spans_of(jaeger, trip_id):
        by_trace[span["trace"]].append(span)
    for spans in by_trace.values():
        if any(span["name"] == PLAN_REQUEST and not span["parent"] for span in spans):
            return spans
    return []


def the_trace_is_one(checks: Checks, jaeger: str, trip: dict) -> str | None:
    print("\n5. Jaeger: one connected trace, from the request to the row")

    def whole() -> list[dict]:
        spans = the_plans_trace(jaeger, trip["trip_id"])
        ids = {span["id"] for span in spans}
        ended = any(span["name"] == "planning run" for span in spans)
        return spans if ended and all(span["parent"] in ids or not span["parent"] for span in spans) else []

    spans = until(whole, SETTLE_S) or the_plans_trace(jaeger, trip["trip_id"])
    if not checks.expect(
        bool(spans), "the request that asked for the plan is the root of a trace, found by the trip's id"
    ):
        print(
            f"     nothing tagged trip.id={trip['trip_id']} — was the backend started with OTEL_EXPORTER_OTLP_ENDPOINT?"
        )
        return None

    ids = {span["id"]: span for span in spans}
    roots = [span for span in spans if not span["parent"]]
    orphans = [span for span in spans if span["parent"] and span["parent"] not in ids]
    services = collections.Counter(span["service"] for span in spans)
    checks.expect(
        len(roots) == 1 and roots[0]["name"] == PLAN_REQUEST,
        "it has one root: " + PLAN_REQUEST,
        f"{len(spans)} spans in trace {spans[0]['trace']}",
    )
    checks.expect(
        not orphans, "no orphaned spans: every span's parent is in the trace", ", ".join(o["name"] for o in orphans[:4])
    )
    checks.expect(
        set(services) == {BACKEND, TOOL_SERVER},
        "it spans both processes",
        ", ".join(f"{service} {count}" for service, count in services.most_common()),
    )

    def under(span: dict, name: str) -> bool:
        climbed = set()  # a trace is a tree; whatever is handed in here is climbed once, not for ever
        while span["parent"] in ids and span["id"] not in climbed:
            climbed.add(span["id"])
            span = ids[span["parent"]]
            if span["name"] == name:
                return True
        return False

    def has(what: str, found: list[dict], detail: str = "") -> None:
        checks.expect(
            bool(found), what, detail or f"{len(found)} span(s), e.g. {found[0]['name']!r}" if found else "none"
        )

    statements = [span for span in spans if span["attributes"].get("db.system") == "postgresql"]
    commands = [span for span in spans if span["attributes"].get("db.system") == "redis"]
    sent = [span for span in spans if span["name"].startswith("mcp.call ")]
    answered = [span for span in spans if span["name"].startswith("tools/call ")]
    models = [span for span in spans if span["name"].startswith("llm ")]
    run = [span for span in spans if span["name"] == "planning run"]
    has("FastAPI: the request", roots)
    has("SQLAlchemy: its statements", statements, f"{len(statements)} statements")
    has(
        "Redis: its commands — in both processes",
        commands,
        ", ".join(f"{s} {c}" for s, c in collections.Counter(span["service"] for span in commands).items()),
    )
    checks.expect(
        bool(answered)
        and all(ids.get(span["parent"], {}).get("name", "").startswith("mcp.call ") for span in answered),
        "MCP: each call the tool server answered is the child of the call the backend made",
        f"{len(sent)} calls sent, {len(answered)} answered in {TOOL_SERVER}",
    )
    has("the models: each call a span", models, ", ".join(sorted({span["name"] for span in models})))
    checks.expect(
        len(run) == 1 and run[0]["parent"] == roots[0]["id"] and run[0]["ended"] > roots[0]["ended"],
        "the run is the request's child, and outlives it",
        f"request {(roots[0]['ended'] - roots[0]['began']) / 1e6:.0f} ms, run {(run[0]['ended'] - run[0]['began']) / 1e9:.1f} s"
        if run
        else "",
    )
    written = [
        span
        for span in statements
        if under(span, "persist")
        and str(span["attributes"].get("db.statement", "")).startswith("INSERT INTO itineraries")
    ]
    has("…down to the row: INSERT INTO itineraries, under the span that saves the plan", written, "persist → INSERT")
    untagged = [span for span in spans if span["attributes"].get("trip.id") != trip["trip_id"]]
    checks.expect(
        not untagged,
        "every span carries the trip's id",
        f"{len(untagged)} do not, e.g. {untagged[0]['name']!r}" if untagged else f"trip.id on all {len(spans)}",
    )
    everything = "".join(span["text"] for span in spans_of(jaeger, trip["trip_id"]))
    checks.expect(
        trip["token"] not in everything and "Bearer " not in everything,
        "nothing Jaeger holds for the trip has the login token in it",
    )
    return spans[0]["trace"]


# ── 6. The log lines ───────────────────────────────────────────────────────


def the_log_lines_are_found(checks: Checks, loki: str, trip: dict, trace_id: str | None) -> None:
    print("\n6. Loki: the trip's log lines, from every service")
    search = '{job="tripplanner"} | trip_id="%s"' % trip["trip_id"]

    def lines() -> list[dict]:
        found = get_json(
            f"{loki}/loki/api/v1/query_range",
            params={
                "query": search,
                "limit": 5000,
                "start": str(int((time.time() - 1800) * 1e9)),
                "direction": "forward",
            },
        )
        rows = []
        for stream in found["data"]["result"]:
            for _moment, line in stream["values"]:
                rows.append({**stream["stream"], "line": line})
        return rows

    def both() -> list[dict]:
        rows = lines()
        return rows if {BACKEND, TOOL_SERVER} <= {row.get("service") for row in rows} else []

    rows = until(both, SETTLE_S) or lines()
    by_service = collections.Counter(row.get("service", "?") for row in rows)
    print(f"     {search}")
    checks.expect(bool(rows), "searching by trip_id finds its lines", f"{len(rows)} lines")
    checks.expect(
        {BACKEND, TOOL_SERVER} <= set(by_service),
        "from every service the plan touched",
        ", ".join(f"{service} {count}" for service, count in by_service.most_common()),
    )
    anonymous = [row for row in rows if row.get("user_id") != trip["user_id"]]
    # one request was made without a login, on purpose (step 3): its line says so, by having nobody's id
    refused = [row for row in anonymous if not row.get("user_id") and '"status": 401' in row["line"]]
    checks.expect(
        len(rows) > len(anonymous) and anonymous == refused and len(refused) <= 1,
        "every one of them carries the user's id too — but the request made without a login",
        f"{len(rows) - len(anonymous)} with the user's id, {len(refused)} with none (the 401)"
        if anonymous == refused
        else f"{len(anonymous)} lines are somebody else's or nobody's",
    )
    if trace_id:
        of_the_trace = [row for row in rows if row.get("trace_id") == trace_id]
        checks.expect(
            {BACKEND, TOOL_SERVER} <= {row.get("service") for row in of_the_trace},
            "and the trace it belongs to: the plan's trace id is on lines of both services",
            f"{len(of_the_trace)} lines with trace_id {trace_id}",
        )
    not_json = [row["line"][:80] for row in rows if not row["line"].lstrip().startswith("{")]
    checks.expect(not not_json, "every line is one JSON object", str(not_json[:2]) if not_json else "")
    checks.expect(not any(trip["token"] in row["line"] for row in rows), "none has the login token in it")


# ── 7. The dashboard ───────────────────────────────────────────────────────

MUST_SHOW = (  # panels that have something to show once a trip has been planned
    "Requests",
    "Duration, p95",
    "Cache hit ratio",
    "Model calls",
    "Request rate",
    "Every endpoint, over the last five minutes",
    "Planning runs",
    "Itinerary builds",
    "Model calls by agent",
    "Tokens",
    "Tool calls",
    "Cache hits by cache",
    "Log lines",
    # Phase 34
    "Alerts firing",
    "Plans, p99 (10 min)",
    "Model calls failing",
    "Model cost, an hour",
    "Embeddings owed",
    "Embedding queue",
    "Model cost",
)
ALERTS = ("PlanLatencyHigh", "EndpointErrorRateHigh", "CacheHitRatioLow", "ModelErrorRateHigh")


# ── 8. The alerts (Phase 34) ───────────────────────────────────────────────


def the_alerts_are_watched(checks: Checks, where: argparse.Namespace, instance: str | None, planned: bool) -> None:
    print("\n8. Alerts: the four rules, evaluated, handed to Alertmanager — and quiet while nothing is wrong")
    try:
        groups = get_json(f"{where.prometheus}/api/v1/rules", params={"type": "alert"})["data"]["groups"]
    except Exception as exc:
        checks.expect(False, "Prometheus has the alert rules", f"{type(exc).__name__}: {exc}")
        return
    rules = {rule["name"]: rule for group in groups for rule in group["rules"]}
    checks.expect(set(rules) == set(ALERTS), "Prometheus has the four alerts", ", ".join(sorted(rules)) or "none")
    broken = {name: rule.get("lastError") for name, rule in rules.items() if rule.get("health") != "ok"}
    checks.expect(not broken, "each is evaluated without an error", str(broken)[:160] if broken else "health ok")
    managers = get_json(f"{where.prometheus}/api/v1/alertmanagers")["data"]["activeAlertmanagers"]
    checks.expect(
        bool(managers), "Prometheus sends what fires to Alertmanager", managers[0]["url"] if managers else "none"
    )
    try:
        status = get_json(f"{where.alertmanager}/api/v2/status")
        receivers = sorted(re.findall(r"^- name: (\S+)", status["config"]["original"], flags=re.M))
        checks.expect(bool(receivers), "Alertmanager knows where to send it", ", ".join(receivers))
    except Exception as exc:
        checks.expect(False, "Alertmanager knows where to send it", f"{type(exc).__name__}: {exc}")
    if instance is not None:
        # an alert is about a backend by its job (several of them say nothing narrower)
        up = get_json(f"{where.prometheus}/api/v1/query", params={"query": f'up{{instance="{instance}"}}'})
        job = (up["data"]["result"] or [{"metric": {}}])[0]["metric"].get("job")
        firing = [
            alert["labels"]["alertname"]
            for alert in get_json(f"{where.prometheus}/api/v1/alerts")["data"]["alerts"]
            if alert["labels"].get("job") == job and alert["state"] == "firing"
        ]
        checks.expect(
            job is not None and not firing,
            "nothing fires for this backend" + (" — after a healthy plan" if planned else ""),
            f"job {job}" + (f": {', '.join(firing)}" if firing else ""),
        )


# ── 9. GET /admin/health-deep (Phase 34) ───────────────────────────────────


def the_deep_check_answers(checks: Checks, where: argparse.Namespace) -> None:
    print(f"\n9. GET /admin/health-deep: every dependency asked to do its work  ({where.api})")
    key = {"Authorization": f"Bearer {where.ops_token}"} if where.ops_token else {}
    try:
        answered = httpx.get(f"{where.api}/admin/health-deep", headers=key, timeout=30)
        body = answered.json()
    except Exception as exc:
        checks.expect(False, "it answers", f"{type(exc).__name__}: {exc}")
        return
    found = body.get("dependencies") or {}
    checks.expect(
        answered.status_code == 200 and body.get("status") in ("ok", "degraded"),
        "it answers, and says the app can work",
        f"HTTP {answered.status_code}, {body.get('status')} in {body.get('took_ms')} ms",
    )
    for name in ("postgres", "redis", "tool_server"):
        result = found.get(name) or {}
        checks.expect(
            result.get("status") == "ok",
            f"{name}: {result.get('detail') or 'not reported'}",
            f"{result.get('latency_ms')} ms" if result.get("status") == "ok" else str(result)[:160],
        )
    langsmith = found.get("langsmith") or {}
    checks.expect(
        langsmith.get("status") in ("ok", "off"),
        f"langsmith: {langsmith.get('status')} — {langsmith.get('detail')}",
        "",
    )
    if where.ops_token:
        refused = httpx.get(f"{where.api}/admin/health-deep", timeout=30)
        checks.expect(
            refused.status_code == 404, "…and to nobody without the operations token", f"HTTP {refused.status_code}"
        )


def the_dashboard_shows_it(checks: Checks, grafana: str, trip: dict | None) -> None:
    print("\n7. Grafana: the dashboard, and what each panel is answered")
    try:
        board = get_json(f"{grafana}/api/dashboards/uid/tripplanner")["dashboard"]
    except Exception as exc:
        checks.expect(False, "the dashboard is there", f"{type(exc).__name__}: {exc}")
        return
    panels = [panel for panel in board["panels"] if panel["type"] != "row"]
    checks.expect(
        len(panels) >= 15, f"“{board['title']}” is provisioned", f"{len(panels)} panels — {grafana}/d/tripplanner"
    )

    def fill(expression: str) -> str:
        expression = expression.replace("${trip_id}", trip["trip_id"] if trip else "").replace("$job", "tripplanner.*")
        return expression.replace("$__rate_interval", "1m")

    errors, empty, shown = [], [], []
    for panel in panels:
        source = panel["datasource"]
        queries = []
        for target in panel["targets"]:
            asked = {
                "refId": target["refId"],
                "datasource": source,
                "expr": fill(target["expr"]),
                "intervalMs": 15000,
                "maxDataPoints": 200,
            }
            asked.update(
                {"queryType": "range", "maxLines": 50}
                if source["type"] == "loki"
                else {"instant": True, "range": False}
            )
            queries.append(asked)
        answer = httpx.post(
            f"{grafana}/api/ds/query", json={"from": "now-15m", "to": "now", "queries": queries}, timeout=30
        )
        results = (
            answer.json().get("results", {})
            if answer.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        failed = [f"{ref}: {result.get('error')}" for ref, result in results.items() if result.get("error")]
        if answer.status_code != 200 or failed or not results:
            errors.append(f"{panel['title']}: HTTP {answer.status_code} {'; '.join(failed)[:120]}")
            continue
        rows = sum(
            len(frame["data"]["values"][0]) if frame.get("data", {}).get("values") else 0
            for result in results.values()
            for frame in result.get("frames", [])
        )
        (shown if rows else empty).append(panel["title"])
    checks.expect(not errors, "every panel's query is answered without an error", "; ".join(errors[:3]))
    if trip is not None:
        missing = sorted(set(MUST_SHOW) & set(empty))
        checks.expect(
            not missing,
            "the panels a planned trip fills have data",
            f"{len(shown)} of {len(panels)} panels show data" if not missing else f"empty: {missing}",
        )
    if empty:
        print(f"     with nothing to show right now: {', '.join(sorted(set(empty)))}")


# ── main ───────────────────────────────────────────────────────────────────


def prodlike_token() -> str:
    env = ROOT / ".env.prodlike"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith("PRODLIKE_OPS_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan a trip, then ask the observability stack what it saw.")
    parser.add_argument("--api", default="http://localhost:8000", help="the backend (default http://localhost:8000)")
    parser.add_argument("--prodlike", action="store_true", help="the production-like stack's backend, with its token")
    parser.add_argument("--ops-token", default="", help="the backend's OPS_TOKEN, when its metrics are closed")
    parser.add_argument(
        "--skip-plan", action="store_true", help="plan nothing: the stack, the metrics' door, the dashboard"
    )
    parser.add_argument("--prometheus", default="http://localhost:9090")
    parser.add_argument("--jaeger", default="http://localhost:16686")
    parser.add_argument("--loki", default="http://localhost:3110")
    parser.add_argument("--grafana", default="http://localhost:3001")
    parser.add_argument("--alertmanager", default="http://localhost:9093")
    parser.add_argument("--receiver", default="http://localhost:9011", help="the stand-in alerts are sent to")
    where = parser.parse_args()
    if where.prodlike:
        where.api, where.ops_token = "http://localhost:8200", where.ops_token or prodlike_token()
    where.api = where.api.rstrip("/")

    checks = Checks()
    if the_stack_answers(checks, where):
        instance = the_backend_shows_its_metrics(checks, where)
        trip = None
        if instance and not where.skip_plan:
            trip = a_trip_is_planned(checks, where.api)
            if trip:
                the_metrics_show_it(checks, where.prometheus, instance)
                trace_id = the_trace_is_one(checks, where.jaeger, trip)
                the_log_lines_are_found(checks, where.loki, trip, trace_id)
        the_dashboard_shows_it(checks, where.grafana, trip)
        the_alerts_are_watched(checks, where, instance, planned=trip is not None)
        the_deep_check_answers(checks, where)

    print(f"\n{checks.passed} passed, {checks.failed} failed")
    return 0 if checks.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

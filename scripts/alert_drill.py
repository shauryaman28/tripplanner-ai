#!/usr/bin/env python
"""Each alert made to fire on purpose, on the running stacks — and kept quiet while nothing is wrong (Phase 34).

    python scripts/chaos.py up                 # the load test's stack: stand-ins for every model and provider
    python scripts/observability.py up         # Prometheus with the alert rules, Alertmanager, the alert receiver

    python scripts/alert_drill.py quiet           healthy traffic for 12 minutes: no alert may fire
    python scripts/alert_drill.py slow-plans      the model that writes itineraries stops answering → PlanLatencyHigh
    python scripts/alert_drill.py errors          the database's pooler is stopped               → EndpointErrorRateHigh
    python scripts/alert_drill.py cache           the cache emptied as fast as it is filled      → CacheHitRatioLow
    python scripts/alert_drill.py model-errors    both short-prompt models answer 429            → ModelErrorRateHigh
    python scripts/alert_drill.py all             the five, one after another: about an hour and a quarter

A drill is what an incident is, in order: travellers using the app as usual; something going wrong
underneath (the faults of Phases 31–32, nothing in the app switched); the alert pending, then
firing in Prometheus; the notification reaching the receiver, with a message that says which
endpoint or metric, what it is now and what the threshold is; the fault mended; the alert resolved,
and that said too. Every step is timed and kept — with every other alert that moved meanwhile, and
each condition's measure every 15 seconds — in docs/alerts/<label>/<drill>.json.

Nothing here spends a quota: it refuses any stack but the load test's, whose models and providers
are stand-ins (scripts/chaos.py's check). A drill takes 10 to 25 minutes, most of it the alert's own
windows: a rate over five minutes, a p99 over ten, and each alert's `for:`. Run it awake (lid open):
Prometheus does not evaluate anything while the machine sleeps, and the record says if it did.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
PROMETHEUS = "http://localhost:9090"
ALERTMANAGER = "http://localhost:9093"
RECEIVER = "http://localhost:9011"
JOB = "tripplanner-prodlike"  # how Prometheus names the stack's backend (docker/observability/prometheus.yml)
RULES = ROOT / "docker" / "observability" / "alerts" / "rules.yml"
RECORDS = ROOT / "docs" / "alerts"
POOLER = "tripplanner-prodlike-pooler-1"

ALERTS = ("PlanLatencyHigh", "EndpointErrorRateHigh", "CacheHitRatioLow", "ModelErrorRateHigh")
PROBES = "/ping|/ready|/version|/admin/health-deep|/admin/sentry-test|none"
# What each alert's condition measures, without its threshold — the same expressions as rules.yml, for
# this backend: kept every 15 s, so a record says how near the threshold things were, not only whether.
MEASURES = {
    "PlanLatencyHigh": (
        f'histogram_quantile(0.99, sum by (le) (rate(planning_run_duration_seconds_bucket{{job="{JOB}",kind="plan"}}[10m])))'
    ),
    "EndpointErrorRateHigh": (
        f'max(sum by (method, handler) (rate(http_requests_total{{job="{JOB}",status="5xx",handler!~"{PROBES}"}}[5m]))'
        f' / sum by (method, handler) (rate(http_requests_total{{job="{JOB}",handler!~"{PROBES}"}}[5m])))'
    ),
    "CacheHitRatioLow": f'min(mcp_cache_hit_ratio{{job="{JOB}"}})',
    "cache lookups in the ratio": f'max(mcp_cache_window_lookups{{job="{JOB}"}})',
    "ModelErrorRateHigh": f'sum(rate(llm_calls_total{{job="{JOB}",status="error"}}[5m])) * 60',
}
THRESHOLDS = {"PlanLatencyHigh": 30, "EndpointErrorRateHigh": 0.05, "CacheHitRatioLow": 0.3, "ModelErrorRateHigh": 10}

DESTINATIONS = ("Jaipur", "Udaipur", "Goa", "Kochi", "Varanasi", "Amritsar", "Mysuru", "Rishikesh")
TRAVELLERS = 6
POLL_S = 5


def _script(name: str):
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"scripts_{name}"] = module
    spec.loader.exec_module(module)
    return module


chaos = _script("chaos")
loadtest = chaos.loadtest


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Asking the stack ───────────────────────────────────────────────────────


def prometheus(path: str, **params) -> dict:
    answer = httpx.get(f"{PROMETHEUS}{path}", params=params, timeout=15)
    answer.raise_for_status()
    return answer.json()["data"]


def measure(expression: str) -> float | None:
    """One number: the expression's value now, or None when it has none (nothing to divide, nothing seen)."""
    try:
        result = prometheus("/api/v1/query", query=expression)["result"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if value != value else round(value, 4)  # NaN is no value


def alerts_now() -> list[dict]:
    """The alerts Prometheus has for this backend right now: pending or firing, each with its labels and message."""
    found = prometheus("/api/v1/alerts")["alerts"]
    return [alert for alert in found if alert["labels"].get("job") == JOB]


def received(alertname: str, status: str, since: str) -> list[dict]:
    """The notifications the receiver was sent for this alert, in this state, since then — each with its alerts."""
    answer = httpx.get(f"{RECEIVER}/alerts", params={"alertname": alertname, "status": status}, timeout=10)
    answer.raise_for_status()
    return [notification for notification in answer.json() if notification["received_at"] >= since]


def rules_are_loaded() -> tuple[bool, str]:
    """Prometheus has the four rules of rules.yml, each evaluated without an error."""
    groups = prometheus("/api/v1/rules", type="alert")["groups"]
    loaded = {rule["name"]: rule for group in groups for rule in group["rules"]}
    written = {rule["alert"] for group in yaml.safe_load(RULES.read_text())["groups"] for rule in group["rules"]}
    if set(loaded) != written:
        return False, f"Prometheus has {sorted(loaded)}, rules.yml says {sorted(written)} — restart it to reload"
    broken = {name: rule.get("lastError") for name, rule in loaded.items() if rule["health"] == "err"}
    return not broken, f"{len(loaded)} rules" + (f", failing: {broken}" if broken else ", none failing")


def ready() -> bool:
    if not chaos.ready():
        return False
    problems = []
    try:
        ok, said = rules_are_loaded()
        if not ok:
            problems.append(said)
        targets = prometheus("/api/v1/targets", state="active")["activeTargets"]
        if not any(t["labels"].get("job") == JOB and t["health"] == "up" for t in targets):
            problems.append(f"Prometheus cannot read the stack's backend (job {JOB}) — is its token in place?")
        if not prometheus("/api/v1/alertmanagers")["activeAlertmanagers"]:
            problems.append("Prometheus knows no Alertmanager")
        httpx.get(f"{ALERTMANAGER}/-/ready", timeout=5).raise_for_status()
        httpx.get(f"{RECEIVER}/", timeout=5).raise_for_status()
    except httpx.HTTPError as failed:
        problems.append(f"{type(failed).__name__}: {failed}")
    for problem in problems:
        print(f"not ready: {problem}")
    if problems:
        print("start them with: python scripts/chaos.py up && python scripts/observability.py up")
    return not problems


# ── Travellers, using the app as usual ─────────────────────────────────────


class Traffic(threading.Thread):
    """A visit every `every` seconds, each in a thread of its own: a trip created, a moment to say what
    one wants (`think` — long enough for the trip's searches to have been warmed), the plan asked for,
    and its status read until it is over. Visits overlap, as travellers' do."""

    def __init__(self, travellers: list, clock, every: float, think: float = 8.0, also_list: bool = True) -> None:
        super().__init__(daemon=True)
        self.travellers, self.clock, self.every, self.think, self.also_list = travellers, clock, every, think, also_list
        self.visits: list[dict] = []
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._begun = 0

    def run(self) -> None:
        while not self._halt.is_set():
            threading.Thread(target=self.visit, args=(self._begun,), daemon=True).start()
            self._begun += 1
            self._halt.wait(self.every)

    def stop(self) -> None:
        self._halt.set()

    def visit(self, number: int) -> None:
        traveller = self.travellers[number % len(self.travellers)]
        # another date every visit: a search nobody has cached, as a new traveller's would be
        start = date.today() + timedelta(days=30 + number % 240)
        seen: dict = {"n": number, "at": self.clock.now()}
        try:
            if self.also_list:
                seen["list"] = traveller.ask("GET", "/trips", timeout=20).status_code
            made = traveller.ask(
                "POST",
                "/trips",
                timeout=20,
                json={
                    "destination": DESTINATIONS[number % len(DESTINATIONS)],
                    "start_date": str(start),
                    "end_date": str(start + timedelta(days=3)),
                    "budget": 70_000,
                    "group_size": 2,
                    "interests": ["history", "food"],
                },
            )
            seen["create"] = made.status_code
            if made.status_code == 201:
                trip = made.json()["id"]
                time.sleep(self.think)
                asked_at = time.monotonic()
                asked = traveller.plan(trip)
                seen["plan"] = asked.status_code
                if asked.status_code == 202:
                    status = {}
                    while time.monotonic() - asked_at < 180:
                        time.sleep(2)
                        status = traveller.status(trip)
                        if status.get("status") in ("completed", "failed"):
                            break
                    seen["outcome"] = status.get("status") or status.get("http")
                    seen["seconds"] = round(time.monotonic() - asked_at, 1)
        except httpx.HTTPError as failed:
            seen["error"] = type(failed).__name__
        with self._lock:
            self.visits.append(seen)

    def summary(self, since: float = 0.0, until: float = float("inf")) -> dict:
        """What the visits begun in that time got: how many, the answers, the outcomes, how long a plan took."""
        with self._lock:
            visits = [visit for visit in self.visits if since <= visit["at"] < until]
        counted: dict = {"visits": len(visits)}
        for key in ("list", "create", "plan", "outcome", "error"):
            values = [str(visit[key]) for visit in visits if key in visit]
            if values:
                counted[key] = {value: values.count(value) for value in sorted(set(values))}
        took = sorted(visit["seconds"] for visit in visits if "seconds" in visit)
        if took:
            counted["plan_seconds"] = {"min": took[0], "median": took[len(took) // 2], "max": took[-1]}
        return counted


def travellers(count: int = TRAVELLERS) -> list:
    """Accounts made while everything works: a drill that stops the database still has people to send."""
    return [chaos.Traveller() for _ in range(count)]


# ── Watching the alerts ────────────────────────────────────────────────────


class Watch(threading.Thread):
    """Every few seconds: which alerts are pending or firing; every 15 s, what each condition measures."""

    def __init__(self, clock) -> None:
        super().__init__(daemon=True)
        self.clock = clock
        self.changes: list[dict] = []  # an alert entering a state, or leaving all of them
        self.measures: list[dict] = []
        self._state: dict[tuple, str] = {}
        self._measured = -1e9
        self._halt = threading.Event()
        self._lock = threading.Lock()

    @staticmethod
    def key(alert: dict) -> tuple:
        labels = alert["labels"]
        return (labels["alertname"], labels.get("endpoint") or labels.get("instance") or "")

    def run(self) -> None:
        while not self._halt.is_set():
            self.look()
            self._halt.wait(POLL_S)

    def look(self) -> None:
        """One look: the alerts' states, and — every 15 s — the measures."""
        try:
            now = {self.key(alert): alert for alert in alerts_now()}
        except httpx.HTTPError:
            now = None
        if now is not None:
            with self._lock:
                for key, alert in now.items():
                    if self._state.get(key) != alert["state"]:
                        self._state[key] = alert["state"]
                        self.changes.append(
                            {
                                "t": self.clock.now(),
                                "alert": key[0],
                                "about": key[1],
                                "state": alert["state"],
                                "value": alert.get("value"),
                                "active_at": alert.get("activeAt"),
                            }
                        )
                for key in [key for key in self._state if key not in now]:
                    del self._state[key]
                    self.changes.append({"t": self.clock.now(), "alert": key[0], "about": key[1], "state": "inactive"})
        if self.clock.now() - self._measured >= 15:
            self._measured = self.clock.now()
            self.measures.append({"t": self._measured, **{name: measure(expr) for name, expr in MEASURES.items()}})

    def stop(self) -> None:
        self._halt.set()

    def state(self, alertname: str) -> str:
        with self._lock:
            states = [state for (name, _), state in self._state.items() if name == alertname]
        return "firing" if "firing" in states else "pending" if "pending" in states else "inactive"

    def first(self, alertname: str, state: str, after: float = 0.0) -> float | None:
        with self._lock:
            times = [
                c["t"] for c in self.changes if c["alert"] == alertname and c["state"] == state and c["t"] >= after
            ]
        return times[0] if times else None

    def others(self, alertname: str) -> list[dict]:
        """Every other alert that moved while this one was drilled."""
        with self._lock:
            return [change for change in self.changes if change["alert"] != alertname]

    def highest(self, name: str, since: float = 0.0, until: float = float("inf")) -> float | None:
        values = [m[name] for m in self.measures if m.get(name) is not None and since <= m["t"] < until]
        return max(values) if values else None

    def lowest(self, name: str, since: float = 0.0, until: float = float("inf")) -> float | None:
        values = [m[name] for m in self.measures if m.get(name) is not None and since <= m["t"] < until]
        return min(values) if values else None


def until(condition, seconds: float, every: float = POLL_S):
    deadline = time.monotonic() + seconds
    while True:
        answer = condition()
        if answer or time.monotonic() >= deadline:
            return answer
        time.sleep(every)


def quiet_before(alertname: str, seconds: float = 900) -> bool:
    """Wait for an alert the last drill may have left firing to be over — a drill begins with it inactive."""

    def gone() -> bool:
        try:
            return not [alert for alert in alerts_now() if alert["labels"]["alertname"] == alertname]
        except httpx.HTTPError:
            return False

    if gone():
        return True
    print(
        f"  {alertname} is still active from before — waiting for it to end (up to {seconds / 60:.0f} min)…", flush=True
    )
    return bool(until(gone, seconds, every=10))


# ── What a message must say ────────────────────────────────────────────────


def deep_check() -> dict:
    """GET /admin/health-deep of the stack's backend, with its operations token: what it says, and its status code."""
    try:
        answer = httpx.get(
            f"{chaos.API}/admin/health-deep",
            headers={"Authorization": f"Bearer {loadtest.ops_token()}"},
            timeout=30,
        )
        return {"http": answer.status_code, **answer.json()}
    except (httpx.HTTPError, ValueError) as failed:
        return {"http": None, "error": f"{type(failed).__name__}: {failed}"}


def message_has_context(alert: dict, expect_endpoint: bool) -> list[str]:
    """What is missing from an alert as it was delivered, for someone who was not looking. Empty: nothing."""
    labels, notes = alert.get("labels") or {}, alert.get("annotations") or {}
    summary, description = notes.get("summary") or "", notes.get("description") or ""
    missing = []
    if "threshold" not in summary:
        missing.append("the threshold, in the summary")
    if "Metric:" not in description or not labels.get("metric"):
        missing.append("which metric")
    if "Now:" not in description or "threshold:" not in description:
        missing.append("its value now, against the threshold")
    if expect_endpoint and not (labels.get("endpoint") and labels["endpoint"] in summary):
        missing.append("which endpoint")
    if "?" in description.replace("?var-job", ""):
        missing.append("a number the message could not find ('?')")
    if not (notes.get("runbook_url") or "").startswith("https://") or "runbook.md#" not in notes["runbook_url"]:
        missing.append("a runbook link")
    return missing


# ── A drill ────────────────────────────────────────────────────────────────


class Drill:
    def __init__(
        self,
        name: str,
        alertname: str,
        cause: str,
        every: float,
        fault_on,
        fault_off,
        expect_endpoint: bool = False,
        warmup_s: float = 90,
        fire_within_s: float = 900,
        resolve_within_s: float = 1200,
        think: float = 8.0,
    ) -> None:
        self.name, self.alertname, self.cause, self.every = name, alertname, cause, every
        self.fault_on, self.fault_off, self.expect_endpoint = fault_on, fault_off, expect_endpoint
        self.warmup_s, self.fire_within_s, self.resolve_within_s, self.think = (
            warmup_s,
            fire_within_s,
            resolve_within_s,
            think,
        )

    def run(self, label: str) -> int:
        print(f"\n── {self.name}: {self.cause} → {self.alertname} ──", flush=True)
        if not chaos.fresh() or not quiet_before(self.alertname):
            print("  the stack could not be made ready for the drill.")
            return 1
        clock, seen = chaos.Clock(), chaos.Seen()
        since = now_utc()
        people = travellers()
        watch = Watch(clock)
        watch.start()
        traffic = Traffic(people, clock, every=self.every, think=self.think)
        traffic.start()
        time.sleep(self.warmup_s)
        seen.expect(
            watch.state(self.alertname) == "inactive",
            f"{self.warmup_s:.0f} s of travellers as usual: {self.alertname} is quiet",
            json.dumps(traffic.summary()),
        )

        broke_at = clock.now()
        fault = self.fault_on()
        seen.note(f"{broke_at:.0f} s: {self.cause}")

        def delivered() -> list[dict]:
            return received(self.alertname, "firing", since) if watch.state(self.alertname) == "firing" else []

        firing = until(delivered, self.fire_within_s)
        delivered_at = clock.now() if firing else None
        deep = deep_check()  # what GET /admin/health-deep says while it is broken
        fired_at = watch.first(self.alertname, "firing", after=broke_at)
        pending_at = watch.first(self.alertname, "pending", after=broke_at)
        mended_at = clock.now()
        self.fault_off(fault)
        seen.note(f"{mended_at:.0f} s: mended")

        seen.note(
            f"GET /admin/health-deep while broken: HTTP {deep.get('http')} {deep.get('status')} — "
            + ", ".join(f"{name} {result.get('status')}" for name, result in (deep.get("dependencies") or {}).items())
        )
        seen.expect(
            pending_at is not None,
            "Prometheus: pending",
            f"{pending_at - broke_at:.0f} s after" if pending_at else "never",
        )
        seen.expect(
            fired_at is not None, "Prometheus: firing", f"{fired_at - broke_at:.0f} s after" if fired_at else "never"
        )
        delivered_alerts = [alert for notification in firing or [] for alert in notification["alerts"]]
        seen.expect(
            bool(delivered_alerts),
            "the receiver was sent it",
            f"{len(firing or [])} notification(s), {len(delivered_alerts)} alert(s), "
            f"{delivered_at - broke_at:.0f} s after it broke"
            if firing
            else f"nothing in {self.fire_within_s:.0f} s",
        )
        for alert in delivered_alerts[:3]:
            gaps = message_has_context(alert, self.expect_endpoint)
            about = alert["labels"].get("endpoint") or alert["labels"].get("instance") or ""
            seen.expect(
                not gaps,
                f"its message says what to know {about}".rstrip(),
                "missing: " + ", ".join(gaps) if gaps else "",
            )
        if delivered_alerts:
            print("\n".join(f"      {line}" for line in as_printed(delivered_alerts[0])), flush=True)

        def resolved() -> list[dict]:
            return received(self.alertname, "resolved", since) if watch.state(self.alertname) == "inactive" else []

        over = until(resolved, self.resolve_within_s) if delivered_alerts else []
        resolved_at = watch.first(self.alertname, "inactive", after=mended_at)
        seen.expect(
            bool(over),
            "mended: resolved, and the receiver told",
            f"{resolved_at - mended_at:.0f} s after it was mended" if over and resolved_at else "not within the wait",
        )
        traffic.stop()
        time.sleep(3)
        watch.stop()

        others = watch.others(self.alertname)
        if others:
            seen.note(
                "other alerts that moved: "
                + "; ".join(f"{c['t']:.0f} s {c['alert']} {c['about']} {c['state']}" for c in others)
            )
        record = {
            "alert": self.alertname,
            "cause": self.cause,
            "threshold": THRESHOLDS[self.alertname],
            "timeline_s": {
                "broken": broke_at,
                "pending": pending_at,
                "firing": fired_at,
                "delivered": delivered_at,
                "mended": mended_at,
                "resolved": resolved_at,
            },
            "measure": {
                "before": self._extreme(watch, 0, broke_at),
                "while_broken": self._extreme(watch, broke_at, mended_at),
                "after": self._extreme(watch, mended_at, float("inf")),
            },
            "traffic": {
                "before": traffic.summary(0, broke_at),
                "while_broken": traffic.summary(broke_at, mended_at),
                "after": traffic.summary(mended_at),
            },
            "health_deep_while_broken": deep,
            "delivered": firing or [],
            "resolved": over or [],
            "alert_changes": watch.changes,
            "measures": watch.measures,
        }
        first_delivery = min((n["received_at"] for n in firing or []), default=None)
        record["timeline_s"]["delivered_at_utc"] = first_delivery
        return keep(label, self.name, clock, seen, record)

    def _extreme(self, watch: Watch, since: float, until_t: float) -> dict:
        """How far the condition went: the highest a "high" alert's measure was, the lowest a "low" one's."""
        low = self.alertname == "CacheHitRatioLow"
        pick = watch.lowest if low else watch.highest
        out = {"value": pick(self.alertname, since, until_t), "threshold": THRESHOLDS[self.alertname]}
        if low:
            out["lookups_in_ratio"] = watch.highest("cache lookups in the ratio", since, until_t)
        return out


def keep(label: str, name: str, clock, seen, record: dict) -> int:
    """The record, in docs/alerts/<label>/<name>.json — with whether the machine stayed awake through it."""
    out = RECORDS / label / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    kept = {"drill": name, "began": clock.began_at, "commit": loadtest.commit(), **record}
    kept["expected"] = seen.results
    kept["as_expected"] = not seen.failed
    # Prometheus evaluates nothing while the machine sleeps: a drill slept through says so.
    kept["asleep_s"] = round(clock.awake.asleep_for(), 1)
    kept["timings_hold"] = not clock.awake.slept()
    if clock.awake.slept():
        print(f"  this machine was asleep for {kept['asleep_s']:.0f} s of the drill — no time in it means anything.")
    out.write_text(json.dumps(kept, indent=1, default=str, ensure_ascii=False) + "\n")
    print(
        f"  {'AS EXPECTED' if not seen.failed else f'NOT AS EXPECTED ({len(seen.failed)})'}   → {loadtest.shown(out)}\n",
        flush=True,
    )
    return 0 if not seen.failed else 1


def as_printed(alert: dict) -> list[str]:
    """The alert as the receiver printed it."""
    return _script("alert_receiver").lines_for(alert)


# ── The faults ─────────────────────────────────────────────────────────────


def builder_hangs():
    chaos.set_faults(builder="hang")


def no_fault(_=None):
    chaos.set_faults()


def pooler_stopped():
    chaos.docker("stop", POOLER)


def pooler_started(_=None):
    chaos.docker("start", POOLER)
    chaos.wait_for(chaos.pooler_serves, 60, every=0.5)


class Emptier(threading.Thread):
    """The tool server's cache, emptied every half second: what Redis evicting under memory pressure — or a
    cache nobody can keep entries in — looks like from the app. Only its keys (mcp:*): the conversations
    and the run notes beside them are left alone, so that nothing but the cache goes wrong."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        import redis

        self.client = redis.Redis.from_url(chaos.REDIS_URL, socket_connect_timeout=2, socket_timeout=3)
        self.deleted = 0
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                keys = list(self.client.scan_iter(match="mcp:*", count=500))
                if keys:
                    self.deleted += self.client.delete(*keys)
            except Exception:
                pass
            self._halt.wait(0.5)

    def stop(self) -> None:
        self._halt.set()


def cache_emptied():
    emptier = Emptier()
    emptier.start()
    return emptier


def cache_left_alone(emptier):
    emptier.stop()
    print(f"    the emptier deleted {emptier.deleted} cache entries", flush=True)


def models_refuse():
    chaos.set_faults(gemini="429", groq_small="429")


DRILLS = {
    "slow-plans": lambda: Drill(
        "slow-plans",
        "PlanLatencyHigh",
        "the model that writes itineraries stops answering",
        every=20,
        fault_on=builder_hangs,
        fault_off=no_fault,
        fire_within_s=900,
        resolve_within_s=1200,
    ),
    "errors": lambda: Drill(
        "errors",
        "EndpointErrorRateHigh",
        "the database's pooler is stopped",
        every=4,
        fault_on=pooler_stopped,
        fault_off=pooler_started,
        expect_endpoint=True,
        fire_within_s=600,
    ),
    "cache": lambda: Drill(
        "cache",
        "CacheHitRatioLow",
        "the cache is emptied as fast as it is filled",
        every=6,
        fault_on=cache_emptied,
        fault_off=cache_left_alone,
        fire_within_s=900,
    ),
    "model-errors": lambda: Drill(
        "model-errors",
        "ModelErrorRateHigh",
        "both short-prompt models answer 429",
        every=5,
        fault_on=models_refuse,
        fault_off=no_fault,
        fire_within_s=900,
    ),
}


# ── Nothing wrong ──────────────────────────────────────────────────────────


def quiet(label: str, minutes: float) -> int:
    """Healthy traffic for long enough to fill every alert's window: nothing may fire, or even be pending."""
    print(f"\n── quiet: {minutes:.0f} minutes of travellers as usual → no alert ──", flush=True)
    if not chaos.fresh():
        return 1
    for name in ALERTS:
        if not quiet_before(name):
            return 1
    clock, seen = chaos.Clock(), chaos.Seen()
    people = travellers()
    watch = Watch(clock)
    watch.start()
    traffic = Traffic(people, clock, every=6)
    traffic.start()
    time.sleep(minutes * 60)
    traffic.stop()
    time.sleep(3)
    watch.stop()
    done = traffic.summary()
    seen.expect(not watch.changes, "no alert fired, and none was even pending", json.dumps(watch.changes)[:300])
    seen.expect(done.get("outcome", {}).get("completed", 0) >= 50, "the travellers got their plans", json.dumps(done))
    headroom = {
        "PlanLatencyHigh": watch.highest("PlanLatencyHigh"),
        "EndpointErrorRateHigh": watch.highest("EndpointErrorRateHigh"),
        "CacheHitRatioLow": watch.lowest("CacheHitRatioLow"),
        "cache lookups in the ratio": watch.highest("cache lookups in the ratio"),
        "ModelErrorRateHigh": watch.highest("ModelErrorRateHigh"),
    }
    seen.note("how near each came: " + json.dumps(headroom))
    return keep(
        label,
        "quiet",
        clock,
        seen,
        {
            "minutes": minutes,
            "traffic": done,
            "nearest": headroom,
            "thresholds": THRESHOLDS,
            "measures": watch.measures,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=["quiet", *DRILLS, "all"])
    parser.add_argument("--label", default="run", help="what the records are kept under, in docs/alerts/")
    parser.add_argument("--minutes", type=float, default=12, help="quiet: how long")
    args = parser.parse_args()
    if not ready():
        return 1
    keep_awake = loadtest.kept_awake(2 * 3600)
    try:
        if args.what == "quiet":
            return quiet(args.label, args.minutes)
        if args.what in DRILLS:
            return DRILLS[args.what]().run(args.label)
        codes = [quiet(args.label, args.minutes)] + [DRILLS[name]().run(args.label) for name in DRILLS]
        print(f"{codes.count(0)} of {len(codes)} went as expected.")
        return max(codes)
    finally:
        chaos.set_faults()
        chaos.docker("start", POOLER)
        if keep_awake is not None:
            keep_awake.terminate()


if __name__ == "__main__":
    sys.exit(main())

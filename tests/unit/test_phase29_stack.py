"""Phase 29 — the observability stack's files, and the two scripts that start it and question it.

Prometheus, Grafana, Jaeger, Loki and Alloy are configured by files that only those programs read:
a metric renamed in the app leaves a dashboard panel empty and nothing fails. These tests hold the
files to the app — every metric a panel asks for is one the app exports — and to each other: the
ports the scripts ask are the ports the stack publishes, the token Prometheus reads is the one the
script writes. That the programs accept the files and show what they should is the other half:
scripts/observability_check.py against the running stack (docs/phase29_build_log.md).
"""

import json
import re
import sys
from importlib import util
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from prometheus_client import REGISTRY

from app.main import app  # noqa: F401 — importing the app is what makes its request metrics exist
from src.telemetry import traces

ROOT = Path(__file__).resolve().parents[2]
STACK = ROOT / "docker" / "observability"
COMPOSE = yaml.safe_load((STACK / "compose.yml").read_text())
PROMETHEUS = yaml.safe_load((STACK / "prometheus.yml").read_text())
DATASOURCES = yaml.safe_load((STACK / "grafana/provisioning/datasources/datasources.yml").read_text())["datasources"]
DASHBOARD = json.loads((STACK / "grafana/dashboards/tripplanner.json").read_text())
PANELS = [panel for panel in DASHBOARD["panels"] if panel["type"] != "row"]
ALLOY = (STACK / "config.alloy").read_text()


def script(name: str):
    """A file from scripts/ as a module — they are programs, not a package."""
    spec = util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def published(service: str) -> dict[int, int]:
    """{port on this machine: port in the container} of a service of the stack."""
    return {
        int(host): int(inside)
        for host, inside in (port.split(":") for port in COMPOSE["services"][service].get("ports", []))
    }


def exported() -> set[str]:
    """Every sample name GET /metrics can show, whether or not anything has happened yet."""
    names: set[str] = set()
    for family in REGISTRY.collect():
        if family.type == "counter":
            names.add(f"{family.name}_total")
        elif family.type == "histogram":
            names.update(f"{family.name}{suffix}" for suffix in ("_bucket", "_sum", "_count"))
        elif family.type == "summary":
            names.update(f"{family.name}{suffix}" for suffix in ("_sum", "_count"))
        else:
            names.add(family.name)
    return names


def asked_for(panel: dict) -> list[str]:
    return [target["expr"] for target in panel["targets"]]


# ── The stack ──────────────────────────────────────────────────────────────


def test_five_programs_each_at_a_version_that_was_run():
    images = {name: service["image"] for name, service in COMPOSE["services"].items()}

    assert set(images) == {"prometheus", "grafana", "jaeger", "loki", "alloy"}
    for name, image in images.items():
        assert re.fullmatch(r"[a-z]+/[a-z]+:v?\d+\.\d+\.\d+", image), f"{name}: {image} is not a pinned release"
    assert images["jaeger"].startswith("jaegertracing/jaeger:2.")  # Jaeger 2: the check script speaks its /api/v3


def test_the_stack_is_reached_where_the_scripts_and_the_app_look_for_it():
    assert published("grafana") == {3001: 3000}  # not 3000: that is the frontend's
    assert published("prometheus") == {9090: 9090}
    assert published("jaeger") == {16686: 16686, 4318: 4318}  # the UI and its API; OTLP over HTTP
    assert published("loki") == {3110: 3100}  # not 3100: the browser tests' web server is there
    assert published("alloy") == {}
    taken = {3000, 3100, 8000, 8100, 8200, 3200, 5432, 6379}  # the app's own, the tests', the production-like stack's
    assert not taken & {port for service in COMPOSE["services"] for port in published(service)}

    up = script("observability")
    assert up.READY == {
        "Prometheus": "http://localhost:9090/-/ready",
        "Grafana": "http://localhost:3001/api/health",
        "Jaeger": "http://localhost:16686/",
        "Loki": "http://localhost:3110/ready",
    }
    check = script("observability_check")
    defaults = vars(_defaults(check))
    assert (defaults["prometheus"], defaults["jaeger"]) == ("http://localhost:9090", "http://localhost:16686")
    assert (defaults["loki"], defaults["grafana"]) == ("http://localhost:3110", "http://localhost:3001")
    # …and what the app is told (.env.example) is the port Jaeger takes spans on
    assert "# OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318" in (ROOT / ".env.example").read_text()


def _defaults(check):
    with (
        patch.object(sys, "argv", ["observability_check.py", "--skip-plan"]),
        patch.object(check, "the_stack_answers", return_value=False),
    ):
        import argparse

        seen = {}
        parse = argparse.ArgumentParser.parse_args

        def remember(parser, *args, **kwargs):
            seen["where"] = parse(parser, *args, **kwargs)
            return seen["where"]

        with patch.object(argparse.ArgumentParser, "parse_args", remember):
            check.main()
    return seen["where"]


def test_prometheus_reads_the_backend_wherever_it_runs_and_with_the_token_where_it_must():
    jobs = {job["job_name"]: job for job in PROMETHEUS["scrape_configs"]}

    assert set(jobs) == {"tripplanner-dev", "tripplanner-prodlike"}
    dev, prodlike = jobs["tripplanner-dev"], jobs["tripplanner-prodlike"]
    assert dev["static_configs"][0]["targets"] == ["host.docker.internal:8000"] and "authorization" not in dev
    assert prodlike["static_configs"][0]["targets"] == ["host.docker.internal:8200"]
    # in production the metrics are for whoever has the token: Prometheus sends it as the app expects it
    assert prodlike["authorization"] == {"type": "Bearer", "credentials_file": "/etc/prometheus/secrets/ops_token"}
    for job in jobs.values():  # the dashboard's "Backend" list is made from this label
        assert job["static_configs"][0]["labels"] == {"service": "tripplanner-backend"}
    prometheus = COMPOSE["services"]["prometheus"]
    assert "./secrets:/etc/prometheus/secrets:ro" in prometheus["volumes"]
    assert prometheus["extra_hosts"] == ["host.docker.internal:host-gateway"]  # the same name on Linux
    assert PROMETHEUS["global"]["scrape_interval"] == "5s"
    grafana_reads = next(source for source in DATASOURCES if source["uid"] == "prometheus")
    assert grafana_reads["jsonData"]["timeInterval"] == PROMETHEUS["global"]["scrape_interval"]


def test_the_token_prometheus_reads_is_the_production_like_stacks_own_and_is_not_committed(tmp_path):
    up = script("observability")
    assert up.TOKEN_FOR_PROMETHEUS == STACK / "secrets" / "ops_token"
    assert "docker/observability/secrets/" in (ROOT / ".gitignore").read_text()

    secrets_file, token_file = tmp_path / ".env.prodlike", tmp_path / "secrets" / "ops_token"
    with patch.object(up, "PRODLIKE_SECRETS", secrets_file), patch.object(up, "TOKEN_FOR_PROMETHEUS", token_file):
        up.hand_over_the_token()  # no production-like stack has been made yet
        assert token_file.read_text() == ""

        secrets_file.write_text(
            "# made by scripts/prodlike.py\nPRODLIKE_JWT_SECRET=aaaa\nPRODLIKE_OPS_TOKEN=an-operations-token\n"
        )
        up.hand_over_the_token()
        assert token_file.read_text() == "an-operations-token"
        assert token_file.stat().st_mode & 0o004  # readable by the container's own user, which is not this one

    check = script("observability_check")
    with patch.object(check, "ROOT", tmp_path):
        assert check.prodlike_token() == "an-operations-token"


def test_the_production_like_stack_can_be_told_where_to_send_its_traces():
    backend = yaml.safe_load((ROOT / "docker/prodlike/compose.yml").read_text())["services"]["backend"]

    assert backend["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "${PRODLIKE_OTLP_ENDPOINT:-}"  # off unless said
    assert "LOG_FORMAT" not in backend["environment"]  # JSON without being asked: APP_ENV is production there
    dockerfile = (ROOT / "src/backend/Dockerfile").read_text()
    assert "COPY src/ ./src/" in dockerfile and (ROOT / "src/telemetry/__init__.py").exists()  # the package ships


# ── The log lines' way into Loki ───────────────────────────────────────────


def test_log_lines_are_read_from_the_apps_containers_and_from_a_backend_on_the_host():
    alloy = COMPOSE["services"]["alloy"]

    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in alloy["volumes"]
    assert "../../logs:/var/log/tripplanner:ro" in alloy["volumes"] and "logs/" in (ROOT / ".gitignore").read_text()
    assert '__path__ = "/var/log/tripplanner/*.log"' in ALLOY
    assert 'url = "http://loki:3100/loki/api/v1/push"' in ALLOY
    # only the app's own containers: a compose service called "backend", as the production-like stack's is
    assert re.search(r'compose_service"\]\s+regex\s+= "backend"\s+action\s+= "keep"', ALLOY)
    assert "backend" in yaml.safe_load((ROOT / "docker/prodlike/compose.yml").read_text())["services"]


def test_what_a_line_is_searched_by_is_not_what_its_stream_is_named_by():
    """`service` and `level` have a handful of values: labels. A trip's id has one per trip: a label per trip
    is a stream per trip, which is what brings a log store down."""

    def block(name: str) -> dict[str, str]:
        body = re.search(rf"stage\.{name} \{{\s+(?:values|expressions) = \{{(.*?)\}}", ALLOY, flags=re.S).group(1)
        return dict(re.findall(r"(\w+)\s+= \"(\w*)\"", body))

    assert block("json") == {
        "level": "level",
        "service": "service",
        "trip_id": "trip_id",
        "user_id": "user_id",
        "trace_id": "trace_id",
        "timestamp": "timestamp",
    }  # the fields src/telemetry/logs.py writes, by the names it writes them under
    assert set(block("labels")) == {"level", "service"}
    assert set(block("structured_metadata")) == {"trip_id", "user_id", "trace_id"}
    assert 'source = "timestamp"' in ALLOY  # the moment the line was written, not the moment it was read


def test_grafana_reads_prometheus_and_loki_and_a_log_lines_trace_opens_in_jaeger():
    by_uid = {source["uid"]: source for source in DATASOURCES}

    assert {uid: source["type"] for uid, source in by_uid.items()} == {"prometheus": "prometheus", "loki": "loki"}
    assert by_uid["prometheus"]["url"] == "http://prometheus:9090" and by_uid["prometheus"]["isDefault"] is True
    assert by_uid["loki"]["url"] == "http://loki:3100"
    (link,) = by_uid["loki"]["jsonData"]["derivedFields"]
    assert (link["matcherType"], link["matcherRegex"]) == ("label", "trace_id")
    assert link["url"] == "http://localhost:16686/trace/$${__value.raw}"  # Jaeger's own page for the trace
    grafana = COMPOSE["services"]["grafana"]["environment"]
    assert grafana["GF_AUTH_ANONYMOUS_ENABLED"] == "true" and grafana["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Viewer"
    assert grafana["GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH"] == "/var/lib/grafana/dashboards/tripplanner.json"


# ── The dashboard ──────────────────────────────────────────────────────────


def test_every_metric_a_panel_asks_for_is_one_the_app_exports():
    there = exported()
    asked: dict[str, set[str]] = {}
    for panel in PANELS:
        if panel["datasource"]["uid"] != "prometheus":
            continue
        for expression in asked_for(panel):
            for name in re.findall(r"\b([a-z][a-z0-9_]*)\{", expression):
                asked.setdefault(name, set()).add(panel["title"])

    missing = {name: sorted(panels) for name, panels in asked.items() if name not in there}
    assert not missing, f"panels ask for metrics the app does not export: {missing}"
    assert len(asked) >= 10  # …and the test did read the expressions


def test_the_dashboard_shows_what_the_roadmap_asks_for():
    everything = "\n".join(expression for panel in PANELS for expression in asked_for(panel))
    by_title = {panel["title"]: panel for panel in PANELS}

    # rate, errors, duration — per endpoint
    assert "sum by (handler) (rate(http_requests_total{" in by_title["Request rate"]["targets"][0]["expr"]
    assert (
        'status="5xx"' in by_title["Error rate"]["targets"][0]["expr"]
        and "by (handler)" in by_title["Error rate"]["targets"][0]["expr"]
    )
    assert "http_request_duration_seconds_bucket" in by_title["Duration, p95"]["targets"][0]["expr"]
    table = by_title["Every endpoint, over the last five minutes"]
    assert table["type"] == "table" and len(table["targets"]) == 5  # rate, errors, p50, p95, p99: one row an endpoint
    # the three metrics it names
    for name in ("llm_calls_total", "mcp_cache_hit_ratio", "itinerary_builds_total"):
        assert name in everything, name
    assert "mcp_cache_hit_ratio" in by_title["Cache hit ratio"]["targets"][0]["expr"]
    # the log lines of one trip
    (logs,) = [panel for panel in PANELS if panel["type"] == "logs"]
    assert (
        logs["datasource"]["uid"] == "loki"
        and '{job="tripplanner"} | trip_id=~"${trip_id}.*"' in logs["targets"][0]["expr"]
    )
    assert [variable["name"] for variable in DASHBOARD["templating"]["list"]] == ["job", "trip_id"]
    assert DASHBOARD["uid"] == "tripplanner"  # the check script and Grafana's home page find it by this


def test_no_request_failed_reads_as_zero_percent_and_not_as_no_data():
    by_title = {panel["title"]: panel for panel in PANELS}

    assert "or vector(0)" in by_title["Errors"]["targets"][0]["expr"]
    for title in ("Error rate", "Cache hits by cache"):
        assert re.search(r"\(.+ or .+ \* 0\) / ", by_title[title]["targets"][0]["expr"]), title
    # …and a ratio of nothing is not shown as a number: the tile says there was nothing to divide
    assert by_title["Cache hit ratio"]["fieldConfig"]["defaults"]["noValue"] == "no lookups"
    assert ">= 0" in by_title["Cache hit ratio"]["targets"][0]["expr"]  # NaN is no data


def test_every_panel_can_be_read_without_being_explained():
    for panel in PANELS:
        assert panel["title"] and panel.get("description"), panel.get("title")
        assert panel["datasource"]["uid"] in ("prometheus", "loki"), panel["title"]
        assert all(target.get("expr") for target in panel["targets"]), panel["title"]
        if panel["type"] in ("stat", "timeseries"):
            assert panel["fieldConfig"]["defaults"].get("unit"), f"{panel['title']}: a number with no unit"
        if panel["type"] == "timeseries":
            assert panel["options"]["legend"]["showLegend"] is True, panel["title"]
            placements = [
                value["value"]
                for override in panel["fieldConfig"]["overrides"]
                for value in override["properties"]
                if value["id"] == "custom.axisPlacement"
            ]
            assert "right" not in placements, f"{panel['title']}: two scales on one chart"
        if panel["type"] == "stat":  # the number in the text's colour; the line under it carries the colour
            assert panel["options"]["colorMode"] == "none", panel["title"]
    ids = [panel["id"] for panel in DASHBOARD["panels"]]
    assert len(ids) == len(set(ids))


def test_a_colour_that_means_something_went_wrong_is_never_given_to_what_went_right():
    good, warning, serious, critical = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"
    colours: dict[str, str] = {}
    for panel in PANELS:
        for override in panel.get("fieldConfig", {}).get("overrides", []):
            for value in override["properties"]:
                if value["id"] == "color":
                    colours[override["matcher"]["options"]] = value["value"]["fixedColor"]

    assert {colours[name] for name in ("completed", "success")} == {good}
    assert {colours[name] for name in ("crashed", "failure", "failed (any model)", "failed (any tool)")} == {critical}
    assert {colours[name] for name in ("no_plan", "retry")} == {warning} and colours["interrupted"] == serious
    # the state colours are for states: no other series is drawn in one of them
    others = {name: colour for name, colour in colours.items() if name in ("p50", "p95", "input", "output")}
    assert others and not set(others.values()) & {good, warning, serious, critical}


# ── The script that questions the stack: it can say no ─────────────────────


def otlp(spans: list[dict]) -> dict:
    """Spans as Jaeger's /api/v3 hands them back: grouped by the service that made them."""
    by_service: dict[str, list] = {}
    for span in spans:
        attributes = [
            {"key": key, "value": {"stringValue": value}} for key, value in span.get("attributes", {}).items()
        ]
        by_service.setdefault(span.get("service", traces.BACKEND), []).append(
            {
                "traceId": span.get("trace", "t1"),
                "spanId": span["id"],
                "parentSpanId": span.get("parent", ""),
                "name": span["name"],
                "startTimeUnixNano": str(span.get("began", 1_000)),
                "endTimeUnixNano": str(span.get("ended", 2_000)),
                "attributes": attributes,
            }
        )
    return {
        "result": {
            "resourceSpans": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                    "scopeSpans": [{"scope": {"name": "tripplanner"}, "spans": made}],
                }
                for service, made in by_service.items()
            ]
        }
    }


TRIP = {"trip_id": "3f2a9c1d-5e6b-4a80-91a2-b3c4d5e6f708", "user_id": "u", "token": "eyJ.the-login.token"}


def a_plans_trace() -> list[dict]:
    whose = {"trip.id": TRIP["trip_id"]}
    return [
        {"id": "a", "name": "POST /trips/{trip_id}/plan", "attributes": whose, "ended": 1_500},
        {"id": "b", "parent": "a", "name": "planning run", "attributes": whose, "ended": 9_000},
        {
            "id": "c",
            "parent": "b",
            "name": "SELECT",
            "attributes": {**whose, "db.system": "postgresql", "db.statement": "SELECT trips.id"},
        },
        {"id": "d", "parent": "b", "name": "PUBLISH", "attributes": {**whose, "db.system": "redis"}},
        {"id": "e", "parent": "b", "name": "mcp.call search_flights", "attributes": whose},
        {
            "id": "f",
            "parent": "e",
            "name": "tools/call search_flights",
            "attributes": whose,
            "service": traces.TOOL_SERVER,
        },
        {
            "id": "g",
            "parent": "f",
            "name": "GET",
            "attributes": {**whose, "db.system": "redis"},
            "service": traces.TOOL_SERVER,
        },
        {"id": "h", "parent": "b", "name": "llm openai/gpt-oss-120b", "attributes": whose},
        {"id": "i", "parent": "b", "name": "persist", "attributes": whose},
        {
            "id": "j",
            "parent": "i",
            "name": "INSERT",
            "attributes": {
                **whose,
                "db.system": "postgresql",
                "db.statement": "INSERT INTO itineraries (id, trip_id) VALUES ($1, $2)",
            },
        },
    ]


def questioned(spans: list[dict], capsys) -> tuple[int, str]:
    """Run the check's trace section on these spans. → (how many expectations failed, what it printed)"""
    check = script("observability_check")
    checks = check.Checks()
    with patch.object(check, "get_json", lambda *_args, **_kwargs: otlp(spans)), patch.object(check, "SETTLE_S", 0.0):
        check.the_trace_is_one(checks, "http://jaeger", TRIP)
    return checks.failed, capsys.readouterr().out


def test_the_check_passes_a_whole_trace(capsys):
    failed, said = questioned(a_plans_trace(), capsys)

    assert failed == 0, said
    assert "no orphaned spans" in said and "it spans both processes" in said and "persist → INSERT" in said


@pytest.mark.parametrize(
    ("what", "change", "noticed"),
    [
        (
            "a span whose parent never arrived",
            lambda spans: spans.append(
                {"id": "z", "parent": "nowhere", "name": "SELECT", "attributes": {"trip.id": TRIP["trip_id"]}}
            ),
            "✗ no orphaned spans",
        ),
        (
            "the tool server's half is missing",
            lambda spans: spans.__setitem__(slice(None), [s for s in spans if s.get("service") != traces.TOOL_SERVER]),
            "✗ it spans both processes",
        ),
        (
            "the tool server started a trace of its own",
            lambda spans: spans.__setitem__(5, {**spans[5], "parent": "b"}),
            "✗ MCP: each call the tool server answered",
        ),
        (
            "the run is not the request's child",
            lambda spans: spans.__setitem__(1, {**spans[1], "parent": ""}),
            "✗ the run is the request's child",
        ),
        ("the itinerary was never written", lambda spans: spans.pop(), "✗ …down to the row"),
        (
            "a span does not say whose it is",
            lambda spans: spans.__setitem__(3, {**spans[3], "attributes": {"db.system": "redis"}}),
            "✗ every span carries the trip's id",
        ),
        (
            "the login token is in an attribute",
            lambda spans: spans.__setitem__(
                0, {**spans[0], "attributes": {**spans[0]["attributes"], "http.url": f"/stream?token={TRIP['token']}"}}
            ),
            "✗ nothing Jaeger holds for the trip has the login token",
        ),
        ("no model call is a span", lambda spans: spans.pop(7), "✗ the models: each call a span"),
        ("there is no such trace", lambda spans: spans.clear(), "✗ the request that asked for the plan is the root"),
    ],
)
def test_the_check_says_no_to_a_trace_that_is_not_whole(what, change, noticed, capsys):
    spans = a_plans_trace()
    change(spans)

    failed, said = questioned(spans, capsys)

    assert failed >= 1 and noticed in said, f"{what}: {said}"

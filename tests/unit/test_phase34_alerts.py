"""Phase 34 — the alerts: the rules Prometheus evaluates, where Alertmanager sends what fires, the stand-in
that receives it, and the drill that makes each one fire on the running stack.

Like the dashboard of Phase 29, the rules are files only other programs read: a metric renamed in the app
leaves an alert that can never fire, and nothing fails. These tests hold the files to the app — every
metric a rule or its message asks for is one the app exports — to the roadmap's four conditions and
thresholds, to the runbook (every alert links to its own section), and to each other. That each rule
fires when it should and not when it should not is promtool's to say, from series written by hand
(docker/observability/alerts/tests.yml): run here through Docker, or a promtool on the PATH, and in CI.
"""

import io
import json
import re
import shutil
import subprocess
import sys
import threading
from importlib import util
from pathlib import Path

import httpx
import pytest
import yaml
from prometheus_client import REGISTRY

from app.main import app  # noqa: F401 — importing the app is what makes its request metrics exist

ROOT = Path(__file__).resolve().parents[2]
STACK = ROOT / "docker" / "observability"
RULES = yaml.safe_load((STACK / "alerts" / "rules.yml").read_text())
RULE_TESTS = yaml.safe_load((STACK / "alerts" / "tests.yml").read_text())
ALERTMANAGER = yaml.safe_load((STACK / "alertmanager.yml").read_text())
PROMETHEUS = yaml.safe_load((STACK / "prometheus.yml").read_text())
COMPOSE = yaml.safe_load((STACK / "compose.yml").read_text())
RUNBOOK = (ROOT / "docs" / "runbook.md").read_text()
ALERTS = {rule["alert"]: rule for group in RULES["groups"] for rule in group["rules"]}
DASHBOARD = json.loads((STACK / "grafana/dashboards/tripplanner.json").read_text())
PANELS = [panel for panel in DASHBOARD["panels"] if panel["type"] != "row"]
PROMETHEUS_IMAGE = COMPOSE["services"]["prometheus"]["image"]
ALERTMANAGER_IMAGE = COMPOSE["services"]["alertmanager"]["image"]


def script(name: str):
    spec = util.spec_from_file_location(f"phase34_{name}", ROOT / "scripts" / f"{name}.py")
    module = util.module_from_spec(spec)
    sys.modules[f"phase34_{name}"] = module
    spec.loader.exec_module(module)
    return module


def exported() -> set[str]:
    names: set[str] = set()
    for family in REGISTRY.collect():
        if family.type == "counter":
            names.add(f"{family.name}_total")
        elif family.type == "histogram":  # a message may name the family; a query asks for its series
            names.update(f"{family.name}{suffix}" for suffix in ("", "_bucket", "_sum", "_count"))
        else:
            names.add(family.name)
    return names


PROMQL_WORDS = {"by", "on", "and", "or", "unless", "without", "ignoring", "group_left", "group_right", "bool"}


def metrics_named(expression: str) -> set[str]:
    """The metrics a PromQL expression reads: its names, without functions, keywords, labels or strings."""
    expression = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expression)  # string literals
    expression = re.sub(r"\{[^}]*\}", "", expression)  # label matchers
    expression = re.sub(r"\b(?:by|on|without|ignoring|group_left|group_right)\s*\([^)]*\)", "", expression)
    expression = re.sub(r"\[[^\]]*\]", "", expression)  # ranges
    names = re.findall(r"(?<![\w.])([a-zA-Z_:][a-zA-Z0-9_:]*)\b(?!\s*\()", expression)
    return {name for name in names if name not in PROMQL_WORDS}


def asked_by(rule: dict) -> set[str]:
    """Every metric a rule reads: in its condition, and in the queries its message runs (in backquotes)."""
    queries = re.findall(r"`([^`]*)`", json.dumps(rule["annotations"], ensure_ascii=False))
    named_in_prose = set(re.findall(r"\b([a-z][a-z0-9_]*)\{", rule["annotations"]["description"]))
    return metrics_named(rule["expr"]).union(*(metrics_named(query) for query in queries), named_in_prose)


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading."""
    return re.sub(r"[^a-z0-9 _-]", "", heading.strip().lower()).replace(" ", "-")


# ── The four alerts, as the roadmap has them ───────────────────────────────


def test_there_are_the_roadmaps_four_alerts_each_on_its_metric_at_its_threshold():
    assert set(ALERTS) == {"PlanLatencyHigh", "EndpointErrorRateHigh", "CacheHitRatioLow", "ModelErrorRateHigh"}
    flat = {name: " ".join(rule["expr"].split()) for name, rule in ALERTS.items()}

    # P99 latency of POST /trips/{id}/plan over 30 s — of the plan it starts, not of the 202 that answers it
    assert flat["PlanLatencyHigh"].startswith("histogram_quantile(0.99, ")
    assert 'planning_run_duration_seconds_bucket{kind="plan"}' in flat["PlanLatencyHigh"]
    assert flat["PlanLatencyHigh"].endswith(") > 30")
    # error rate of any endpoint over 5 % over 5 minutes — one alert per endpoint, 5xx of all
    assert flat["EndpointErrorRateHigh"].endswith(") > 0.05")
    assert flat["EndpointErrorRateHigh"].count("sum by (job, method, handler) (rate(http_requests_total{") == 2
    assert 'status="5xx"' in flat["EndpointErrorRateHigh"] and flat["EndpointErrorRateHigh"].count("[5m]") == 2
    # mcp_cache_hit_ratio under 0.3
    assert flat["CacheHitRatioLow"].startswith("mcp_cache_hit_ratio < 0.3 and on (job, instance) ")
    # llm_calls_total{status="error"} over 10 a minute
    assert flat["ModelErrorRateHigh"] == 'sum by (job) (rate(llm_calls_total{status="error"}[5m])) * 60 > 10'


def test_the_plan_the_alert_is_about_is_the_one_post_plan_starts_and_30_s_is_an_edge_of_its_histogram():
    from src.telemetry import metrics

    assert 30 in metrics.RUN_BUCKETS  # "over 30 s" is counted, not guessed between two buckets
    assert "plan" in metrics.RUN_KINDS
    routes = (ROOT / "src/backend/app/api/routes/trips.py").read_text()
    # POST /plan starts a run of kind "plan" when it is not a retry, a refinement or a way out of a conflict
    assert re.search(r'else "replan"\s+if event_extra.get\("choice"\)\s+else "plan"', routes)


def test_every_metric_a_rule_or_its_message_asks_for_is_one_the_app_exports():
    there = exported()
    asked = {name: asked_by(rule) for name, rule in ALERTS.items()}
    missing = {alert: sorted(names - there) for alert, names in asked.items() if names - there}
    assert not missing, f"alerts ask for metrics the app does not export: {missing}"
    # …and the test did read them: the condition's metric and the ones its message counts with
    assert asked["PlanLatencyHigh"] == {
        "planning_run_duration_seconds",  # named in the message
        "planning_run_duration_seconds_bucket",
        "planning_run_duration_seconds_count",
    }
    assert asked["CacheHitRatioLow"] == {"mcp_cache_hit_ratio", "mcp_cache_window_lookups", "mcp_cache_lookups_total"}
    assert asked["EndpointErrorRateHigh"] == {"http_requests_total"} and asked["ModelErrorRateHigh"] == {
        "llm_calls_total"
    }


def test_the_probes_left_out_of_the_error_rate_are_routes_the_app_has():
    from app.main import app as the_app

    flat = " ".join(ALERTS["EndpointErrorRateHigh"]["expr"].split())
    (left_out,) = set(re.findall(r'handler!~"([^"]+)"', flat))
    paths = set(the_app.openapi()["paths"])
    for probe in left_out.split("|"):
        assert probe == "none" or probe in paths, probe  # "none": a request to no route at all
    # the probes — and the route that fails on purpose (Phase 27), whose 500 fired this alert the first time
    # Phase 27's check ran beside it
    assert set(left_out.split("|")) == {
        "/ping",
        "/ready",
        "/version",
        "/admin/health-deep",
        "/admin/sentry-test",
        "none",
    }
    # …and the message lists the same ones as the condition
    assert ALERTS["EndpointErrorRateHigh"]["annotations"]["description"].count(left_out) == 2


def test_the_cache_alerts_floor_is_the_lookups_its_ratio_is_of():
    """A NaN ratio (nothing looked up) is under nothing; the floor is how many lookups the ratio is of.

    It was the counter's five-minute increase, until the quiet drill found that after a restart that
    counts the last process's lookups while the ratio counts only the new one's (tests.yml has the case)."""
    flat = " ".join(ALERTS["CacheHitRatioLow"]["expr"].split())
    assert flat == "mcp_cache_hit_ratio < 0.3 and on (job, instance) mcp_cache_window_lookups >= 10"


def test_the_ratio_and_its_floor_count_the_same_lookups_in_the_same_window():
    from src.telemetry import metrics

    now = [1000.0]
    window = metrics.Window(seconds=300, clock=lambda: now[0])
    assert window.lookups() == 0.0 and window.ratio() != window.ratio()  # NaN: nothing to divide
    window.add(hits=1, lookups=4)
    now[0] += 200
    window.add(hits=3, lookups=6)
    assert (window.ratio(), window.lookups()) == (0.4, 10.0)
    now[0] += 150  # the first four are older than five minutes now: gone from both — each forgets them itself
    assert window.lookups() == 6.0
    assert window.ratio() == 0.5
    # …and the app reports both from its one window: three more lookups are three more in the floor
    before = REGISTRY.get_sample_value("mcp_cache_window_lookups")
    metrics.cache_lookups("flights", "miss", 3)
    assert REGISTRY.get_sample_value("mcp_cache_window_lookups") == before + 3 == metrics.cache_window.lookups()
    assert REGISTRY.get_sample_value("mcp_cache_hit_ratio") == metrics.cache_window.ratio()


# ── What each alert says ───────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(ALERTS))
def test_every_alert_says_what_to_know_and_where_to_go(name):
    rule = ALERTS[name]
    labels, notes = rule["labels"], rule["annotations"]

    assert labels["severity"] in ("warning", "critical")
    assert labels["metric"] in notes["description"]  # which metric — said in the label and in the message
    assert "threshold" in notes["summary"]  # the first line already says it: now, against what
    assert "Now: " in notes["description"] and "threshold: " in notes["description"]
    assert "Look first:" in notes["description"]
    assert re.match(r"\d+m$", rule["for"]), rule["for"]  # a moment over the line is not an incident
    # the runbook's own section for it
    url = notes["runbook_url"]
    assert url == f"https://github.com/shauryaman28/tripplanner-ai/blob/main/docs/runbook.md#{name.lower()}"
    headings = {slug(line[3:]) for line in RUNBOOK.splitlines() if line.startswith("## ")}
    assert name.lower() in headings, f"docs/runbook.md has no section '## {name}'"
    assert notes["dashboard"] == "http://localhost:3001/d/tripplanner?var-job={{ $labels.job }}"


def test_the_endpoint_alert_names_its_endpoint_and_the_plan_alert_names_post_plan():
    assert ALERTS["EndpointErrorRateHigh"]["labels"]["endpoint"] == "{{ $labels.method }} {{ $labels.handler }}"
    assert ALERTS["EndpointErrorRateHigh"]["annotations"]["summary"].startswith(
        "{{ $labels.method }} {{ $labels.handler }}"
    )
    assert ALERTS["PlanLatencyHigh"]["labels"]["endpoint"] == "POST /trips/{trip_id}/plan"


def test_the_runbook_has_a_section_for_each_alert_with_what_to_check_and_what_fixes_it():
    sections = re.split(r"^## ", RUNBOOK, flags=re.M)
    by_name = {section.split("\n", 1)[0].strip(): section for section in sections[1:]}
    for name in ALERTS:
        section = by_name[name]
        for part in ("What it means", "Check first", "Common causes", "Known fixes"):
            assert f"### {part}" in section, f"{name}: no '{part}'"
    assert "/admin/health-deep" in RUNBOOK and "amtool" in RUNBOOK  # the deep check, and how to silence


# ── The dashboard ──────────────────────────────────────────────────────────


def _panel(title: str) -> dict:
    (found,) = [panel for panel in PANELS if panel["title"] == title]
    return found


def _drawn_at(panel: dict) -> list[float]:
    steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
    return [step["value"] for step in steps if step["value"] is not None]


def test_one_dashboard_shows_red_metrics_model_cost_the_cache_the_embedding_queue_and_the_alerts():
    everything = "\n".join(target["expr"] for panel in PANELS for target in panel["targets"])
    # the roadmap's list, all on the one dashboard
    for metric in (
        "http_requests_total",  # rate and errors
        "http_request_duration_seconds_bucket",  # duration
        "llm_cost_usd_total",
        "mcp_cache_hit_ratio",
        "embedding_queue_depth",
        "ALERTS{",
    ):
        assert metric in everything, metric
    first_row = DASHBOARD["panels"][0]
    assert first_row["type"] == "row" and first_row["title"].startswith("Alerts")  # what is wrong comes first
    assert 'ALERTS{alertstate="firing", job=~"$job"}' in _panel("Alerts firing")["targets"][0]["expr"]
    assert _panel("Alerts, firing and pending")["fieldConfig"]["defaults"]["noValue"] == "nothing is firing or pending"
    assert "max by (state) (embedding_queue_depth" in _panel("Embedding queue")["targets"][0]["expr"]
    assert "rate(llm_cost_usd_total" in _panel("Model cost")["targets"][0]["expr"]


def test_the_thresholds_drawn_are_the_alerts_own():
    # the tile is the alert's own measure: p99, of plans, over the same ten minutes
    tile = _panel("Plans, p99 (10 min)")["targets"][0]["expr"]
    for part in ("histogram_quantile(0.99,", "planning_run_duration_seconds_bucket{", 'kind="plan"', "[10m]"):
        assert part in tile and part in ALERTS["PlanLatencyHigh"]["expr"], part
    assert _drawn_at(_panel("Plans, p99 (10 min)")) == [30]
    assert _drawn_at(_panel("How long a plan takes")) == [30]
    assert _drawn_at(_panel("Error rate")) == [0.05]
    assert _drawn_at(_panel("Cache hits by cache")) == [0.3]
    assert _drawn_at(_panel("Model calls by model")) == [10]
    assert _drawn_at(_panel("Model calls failing")) == [10.0001]  # red only *over* 10, as the alert
    for title in ("How long a plan takes", "Error rate", "Cache hits by cache", "Model calls by model"):
        assert _panel(title)["fieldConfig"]["defaults"]["custom"]["thresholdsStyle"] == {"mode": "dashed"}, title


def test_no_two_panels_are_drawn_over_each_other():
    taken: dict[tuple[int, int], str] = {}
    for panel in DASHBOARD["panels"]:
        place = panel["gridPos"]
        assert place["x"] + place["w"] <= 24, panel["title"]
        for x in range(place["x"], place["x"] + place["w"]):
            for y in range(place["y"], place["y"] + place["h"]):
                assert (x, y) not in taken, f"{panel['title']} is drawn over {taken[(x, y)]}"
                taken[(x, y)] = panel["title"]


# ── Where it goes ──────────────────────────────────────────────────────────


def test_prometheus_evaluates_the_rules_and_sends_what_fires_to_alertmanager():
    assert PROMETHEUS["rule_files"] == ["/etc/prometheus/alerts/rules.yml"]
    prometheus = COMPOSE["services"]["prometheus"]
    assert "./alerts:/etc/prometheus/alerts:ro" in prometheus["volumes"]
    (target,) = PROMETHEUS["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert target == "alertmanager:9093" and "9093:9093" in COMPOSE["services"]["alertmanager"]["ports"]
    assert PROMETHEUS["global"]["evaluation_interval"] == "15s"
    assert "--web.external-url=http://localhost:9090" in prometheus["command"]


def test_alertmanager_sends_every_alert_to_the_receiver_grouped_and_says_when_it_stops():
    route = ALERTMANAGER["route"]
    assert set(route["group_by"]) == {"alertname", "job"}  # the database gone: one message, every endpoint in it
    assert "routes" not in route  # nothing is routed elsewhere, or dropped
    (receiver,) = ALERTMANAGER["receivers"]
    assert receiver["name"] == route["receiver"]
    (hook,) = receiver["webhook_configs"]
    assert hook["send_resolved"] is True
    assert hook["url"] == "http://alert-receiver:9011/alerts"
    service = COMPOSE["services"]["alert-receiver"]
    assert service["ports"] == ["9011:9011"] and "--port" in service["command"] and "9011" in service["command"]
    assert "../../scripts/alert_receiver.py:/receiver/alert_receiver.py:ro" in service["volumes"]
    am = COMPOSE["services"]["alertmanager"]
    assert "./alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" in am["volumes"]


def test_the_two_new_programs_are_pinned_and_the_receiver_runs_on_the_apps_own_base_image():
    assert re.fullmatch(r"prom/alertmanager:v\d+\.\d+\.\d+", ALERTMANAGER_IMAGE)
    base = re.search(r"^FROM (\S+)", (ROOT / "src/backend/Dockerfile").read_text(), flags=re.M).group(1)
    assert COMPOSE["services"]["alert-receiver"]["image"] == base  # nothing new to pull
    up = script("observability")
    assert up.READY["Alertmanager"] == "http://localhost:9093/-/ready"
    assert up.READY["the alert receiver"] == "http://localhost:9011/"


# ── promtool and amtool: the rules fire, and keep quiet, on purpose ────────


def _tool(image: str, entrypoint: str, *args: str, mount: Path) -> subprocess.CompletedProcess:
    """Run promtool or amtool: the one on the PATH, or the stack's own image through Docker."""
    if shutil.which(entrypoint):
        return subprocess.run([entrypoint, *args], cwd=mount, capture_output=True, text=True, timeout=120)
    command = ["docker", "run", "--rm", "-v", f"{mount}:/w:ro", "-w", "/w", "--entrypoint", entrypoint, image, *args]
    return subprocess.run(command, capture_output=True, text=True, timeout=180)


def _docker_answers() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_the_tools = pytest.mark.skipif(
    not (shutil.which("promtool") or _docker_answers()),
    reason="needs promtool on the PATH, or Docker to run it from the stack's image (CI runs it either way)",
)


@needs_the_tools
def test_promtool_accepts_the_rules_and_every_one_fires_and_keeps_quiet_as_written():
    checked = _tool(PROMETHEUS_IMAGE, "promtool", "check", "rules", "rules.yml", mount=STACK / "alerts")
    assert checked.returncode == 0 and "SUCCESS: 4 rules found" in checked.stdout, checked.stdout + checked.stderr
    tested = _tool(PROMETHEUS_IMAGE, "promtool", "test", "rules", "tests.yml", mount=STACK / "alerts")
    assert tested.returncode == 0 and "SUCCESS" in tested.stdout, tested.stdout + tested.stderr


@needs_the_tools
def test_amtool_accepts_the_alertmanager_config():
    checked = _tool(ALERTMANAGER_IMAGE, "amtool", "check-config", "alertmanager.yml", mount=STACK)
    assert checked.returncode == 0 and "SUCCESS" in checked.stdout, checked.stdout + checked.stderr


def test_every_alert_is_tested_firing_with_its_whole_message_and_kept_quiet():
    fired: dict[str, int] = {}
    quiet: dict[str, int] = {}
    for test in RULE_TESTS["tests"]:
        for case in test["alert_rule_test"]:
            if case["exp_alerts"]:
                for alert in case["exp_alerts"]:
                    assert set(alert["exp_annotations"]) == set(ALERTS[case["alertname"]]["annotations"])
                fired[case["alertname"]] = fired.get(case["alertname"], 0) + 1
            else:
                quiet[case["alertname"]] = quiet.get(case["alertname"], 0) + 1
    assert set(fired) == set(ALERTS) and set(quiet) == set(ALERTS)
    assert RULE_TESTS["rule_files"] == ["rules.yml"]
    # the bucket labels as the app writes them and Prometheus keeps them — "30.0", which a rule that asked
    # for le="30" would never find (it did, until the running stack said otherwise)
    text = (STACK / "alerts" / "tests.yml").read_text()
    assert 'le="30.0"' in text and not re.search(r'le="\d+"', text)


def test_ci_runs_promtool_and_amtool_on_every_push():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    (step,) = [s for s in ci["jobs"]["backend"]["steps"] if s.get("name", "").startswith("Alert rules")]
    lines = step["run"].strip().splitlines()
    assert len(lines) == 3 and all(
        line.startswith('docker run --rm -v "$PWD/docker/observability:/w:ro"') for line in lines
    )
    assert lines[0].endswith(f"--entrypoint promtool {PROMETHEUS_IMAGE} check rules /w/alerts/rules.yml")
    assert lines[1].endswith(f"--entrypoint promtool {PROMETHEUS_IMAGE} test rules /w/alerts/tests.yml")
    assert lines[2].endswith(f"--entrypoint amtool {ALERTMANAGER_IMAGE} check-config /w/alertmanager.yml")
    assert PROMETHEUS_IMAGE in step["run"] and ALERTMANAGER_IMAGE in step["run"]  # the stack's own versions


# ── The receiver ───────────────────────────────────────────────────────────

receiver_module = script("alert_receiver")


def a_notification(status: str = "firing", name: str = "EndpointErrorRateHigh") -> dict:
    """What Alertmanager posts (webhook, version 4)."""
    rule = ALERTS[name]
    return {
        "version": "4",
        "status": status,
        "receiver": "stand-in",
        "groupLabels": {"alertname": name, "job": "tripplanner-prodlike"},
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": name,
                    "severity": rule["labels"]["severity"],
                    "job": "tripplanner-prodlike",
                    "endpoint": "POST /trips/{trip_id}/plan",
                    "metric": rule["labels"]["metric"],
                },
                "annotations": {
                    "summary": "POST /trips/{trip_id}/plan — 100% of its requests failed over the last 5 minutes; "
                    "the threshold is 5%.",
                    "description": 'Metric: http_requests_total{status="5xx"} ÷ all requests, over 5m.\n'
                    "Now: 100% · threshold: 5% · backend: tripplanner-prodlike.",
                    "runbook_url": rule["annotations"]["runbook_url"],
                },
                "startsAt": "2026-10-16T17:40:05.123Z",
                "endsAt": "2026-10-16T17:52:35Z" if status == "resolved" else "0001-01-01T00:00:00Z",
            }
        ],
    }


def test_a_firing_alert_is_printed_as_the_person_on_call_reads_it_and_a_resolved_one_in_a_line():
    out = io.StringIO()
    receiver = receiver_module.Receiver(out=out)

    assert receiver.take(a_notification("firing")) == 1
    receiver.take(a_notification("resolved"))

    printed = out.getvalue().splitlines()
    assert (
        printed[0] == "▲ FIRING   EndpointErrorRateHigh · critical · tripplanner-prodlike · POST /trips/{trip_id}/plan"
    )
    assert printed[1].strip().startswith("POST /trips/{trip_id}/plan — 100% of its requests failed")
    assert any("Now: 100% · threshold: 5%" in line for line in printed)
    assert printed[-2].strip() == f"Runbook: {ALERTS['EndpointErrorRateHigh']['annotations']['runbook_url']}"
    assert printed[-1] == (
        "✓ RESOLVED EndpointErrorRateHigh · critical · tripplanner-prodlike · POST /trips/{trip_id}/plan"
        "  (fired 17:40:05, resolved 17:52:35)"
    )


def test_what_was_received_is_kept_and_found_by_alert_and_state():
    receiver = receiver_module.Receiver(out=io.StringIO())
    receiver.take(a_notification("firing", "CacheHitRatioLow"))
    receiver.take(a_notification("firing"))
    receiver.take(a_notification("resolved"))

    assert len(receiver.matching()) == 3
    (firing,) = receiver.matching("EndpointErrorRateHigh", "firing")
    assert firing["alerts"][0]["labels"]["alertname"] == "EndpointErrorRateHigh" and firing["received_at"]
    assert len(receiver.matching(status="firing")) == 2
    assert receiver.matching("ModelErrorRateHigh") == []
    with pytest.raises(ValueError):
        receiver.take({"status": "firing"})  # not a notification: no list of alerts
    receiver.forget()
    assert receiver.matching() == []


def test_the_receiver_speaks_alertmanagers_webhook_over_http():
    server = receiver_module.serve(0, receiver_module.Receiver(out=io.StringIO()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert httpx.post(f"{base}/alerts", json=a_notification()).json() == {"alerts": 1}
        assert httpx.post(f"{base}/alerts", content=b"not json").status_code == 400
        assert httpx.post(f"{base}/elsewhere", json={}).status_code == 404
        found = httpx.get(f"{base}/alerts", params={"alertname": "EndpointErrorRateHigh", "status": "firing"}).json()
        assert len(found) == 1 and found[0]["alerts"][0]["annotations"]["summary"].endswith("the threshold is 5%.")
        httpx.post(f"{base}/reset")
        assert httpx.get(f"{base}/alerts").json() == []
    finally:
        server.shutdown()


# ── The drill ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def drill():
    return script("alert_drill")


def _delivered(name: str) -> dict:
    """An alert as it was delivered: the labels and the message promtool's test expects of it."""
    for test in RULE_TESTS["tests"]:
        for case in test["alert_rule_test"]:
            for alert in case["exp_alerts"]:
                if case["alertname"] == name:
                    return {
                        "labels": {"alertname": name, **alert["exp_labels"]},
                        "annotations": alert["exp_annotations"],
                    }
    raise AssertionError(name)


@pytest.mark.parametrize("name", sorted(ALERTS))
def test_the_drill_finds_nothing_missing_from_a_message_as_the_rules_write_it(drill, name):
    assert drill.message_has_context(_delivered(name), expect_endpoint=name == "EndpointErrorRateHigh") == []


def test_the_drill_says_what_a_message_lacks(drill):
    alert = _delivered("EndpointErrorRateHigh")
    alert["annotations"] = {**alert["annotations"], "summary": "Something failed."}
    alert["annotations"]["description"] = alert["annotations"]["description"].replace("Now:", "At present:")
    alert["annotations"]["description"] += "\nRequests: ?"
    del alert["annotations"]["runbook_url"]
    del alert["labels"]["metric"]

    missing = drill.message_has_context(alert, expect_endpoint=True)
    assert missing == [
        "the threshold, in the summary",
        "which metric",
        "its value now, against the threshold",
        "which endpoint",
        "a number the message could not find ('?')",
        "a runbook link",
    ]


def test_the_drill_measures_what_each_rule_measures(drill):
    for name, rule in ALERTS.items():
        assert metrics_named(drill.MEASURES[name]) == metrics_named(rule["expr"]) - {"mcp_cache_window_lookups"}, name
    assert metrics_named(drill.MEASURES["cache lookups in the ratio"]) == {"mcp_cache_window_lookups"}
    assert set(drill.THRESHOLDS) == set(ALERTS)
    flat = {name: " ".join(rule["expr"].split()) for name, rule in ALERTS.items()}
    for name, threshold in drill.THRESHOLDS.items():
        assert f" {threshold:g}" in flat[name] or f" {threshold}" in flat[name], name
    assert drill.PROBES in flat["EndpointErrorRateHigh"]
    assert set(drill.DRILLS) == {"slow-plans", "errors", "cache", "model-errors"}
    assert {drill.DRILLS[name]().alertname for name in drill.DRILLS} == set(ALERTS)  # one drill an alert


def test_the_drill_counts_what_the_travellers_got(drill):
    class Clock:
        def now(self):
            return 0.0

    traffic = drill.Traffic([], Clock(), every=5)
    traffic.visits = [
        {"n": 0, "at": 1.0, "list": 200, "create": 201, "plan": 202, "outcome": "completed", "seconds": 12.0},
        {"n": 1, "at": 6.0, "list": 503, "create": 503},
        {"n": 2, "at": 11.0, "list": 200, "create": 201, "plan": 202, "outcome": "failed", "seconds": 36.4},
        {"n": 3, "at": 16.0, "error": "ReadTimeout"},
    ]
    assert traffic.summary() == {
        "visits": 4,
        "list": {"200": 2, "503": 1},
        "create": {"201": 2, "503": 1},
        "plan": {"202": 2},
        "outcome": {"completed": 1, "failed": 1},
        "error": {"ReadTimeout": 1},
        "plan_seconds": {"min": 12.0, "median": 36.4, "max": 36.4},
    }
    assert traffic.summary(since=5, until=12)["visits"] == 2


def test_the_drill_notes_each_alert_entering_and_leaving_a_state(drill, monkeypatch):
    seen = iter(
        [
            [],
            [{"labels": {"alertname": "CacheHitRatioLow", "instance": "a"}, "state": "pending", "value": "1e-01"}],
            [{"labels": {"alertname": "CacheHitRatioLow", "instance": "a"}, "state": "firing", "value": "1e-01"}],
            [{"labels": {"alertname": "CacheHitRatioLow", "instance": "a"}, "state": "firing", "value": "2e-01"}],
            [],
        ]
    )
    ticks = iter(range(100))

    class Clock:
        def now(self):
            return float(next(ticks))

    watch = drill.Watch(Clock())
    monkeypatch.setattr(drill, "measure", lambda _expr: None)
    for answer in seen:
        monkeypatch.setattr(drill, "alerts_now", lambda answer=answer: answer)
        watch.look()
    states = [change["state"] for change in watch.changes]
    assert states == ["pending", "firing", "inactive"]  # a value that moves while firing is not a change
    assert watch.first("CacheHitRatioLow", "firing") is not None and watch.state("CacheHitRatioLow") == "inactive"


def test_the_drill_refuses_any_stack_but_the_one_with_stand_ins(drill, monkeypatch, capsys):
    monkeypatch.setattr(drill.chaos, "ready", lambda: False)
    assert drill.ready() is False


# ── The check script: section 8 and 9 (scripts/observability_check.py) ─────


@pytest.fixture(scope="module")
def check():
    return script("observability_check")


def _where(check, **more):
    import argparse

    where = {"prometheus": "http://p", "alertmanager": "http://am", "api": "http://api", "ops_token": "tok"}
    return argparse.Namespace(**{**where, **more})


def _answers(routes: dict):
    """httpx.get as the stack would answer these addresses: {address prefix: (status, body)}."""

    def get(url, params=None, **_kwargs):
        for prefix, (status, body) in routes.items():
            if url.startswith(prefix) and (params is None or "query" not in params or prefix.endswith("/query")):
                return httpx.Response(status, json=body, request=httpx.Request("GET", url))
        raise AssertionError(f"not expected: {url} {params}")

    return get


def _alerting(rules: list[dict], firing: list[dict], managers=("http://alertmanager:9093/api/v2/alerts",)) -> dict:
    return {
        "http://p/api/v1/rules": (200, {"data": {"groups": [{"rules": rules}]}}),
        "http://p/api/v1/alertmanagers": (200, {"data": {"activeAlertmanagers": [{"url": u} for u in managers]}}),
        "http://am/api/v2/status": (200, {"config": {"original": "receivers:\n- name: stand-in\n"}}),
        "http://p/api/v1/query": (200, {"data": {"result": [{"metric": {"job": "tripplanner-prodlike"}}]}}),
        "http://p/api/v1/alerts": (200, {"data": {"alerts": firing}}),
    }


FOUR = [{"name": name, "health": "ok"} for name in ALERTS]


def test_the_check_says_yes_to_the_four_rules_watched_and_nothing_firing(check, monkeypatch):
    monkeypatch.setattr(check.httpx, "get", _answers(_alerting(FOUR, [])))
    checks = check.Checks()
    check.the_alerts_are_watched(checks, _where(check), "host.docker.internal:8200", planned=True)
    assert (checks.passed, checks.failed) == (5, 0)


@pytest.mark.parametrize(
    "broken",
    [
        {"rules": FOUR[:3]},  # one rule not loaded
        {"rules": [*FOUR[:3], {"name": "ModelErrorRateHigh", "health": "err", "lastError": "bad query"}]},
        {"managers": ()},  # nothing to send to
        {"firing": [{"labels": {"alertname": "CacheHitRatioLow", "job": "tripplanner-prodlike"}, "state": "firing"}]},
    ],
)
def test_the_check_says_no_to_a_rule_missing_or_failing_no_alertmanager_or_an_alert_firing(check, monkeypatch, broken):
    answers = _alerting(broken.get("rules", FOUR), broken.get("firing", []), broken.get("managers", ("http://am",)))
    monkeypatch.setattr(check.httpx, "get", _answers(answers))
    checks = check.Checks()
    check.the_alerts_are_watched(checks, _where(check), "host.docker.internal:8200", planned=True)
    assert checks.failed == 1


def test_an_alert_firing_for_another_backend_is_not_this_ones(check, monkeypatch):
    elsewhere = [{"labels": {"alertname": "PlanLatencyHigh", "job": "tripplanner-dev"}, "state": "firing"}]
    monkeypatch.setattr(check.httpx, "get", _answers(_alerting(FOUR, elsewhere)))
    checks = check.Checks()
    check.the_alerts_are_watched(checks, _where(check), "host.docker.internal:8200", planned=True)
    assert checks.failed == 0


def _deep(status: str, http: int = 200, **dependencies) -> dict:
    found = {
        name: {"status": "ok", "detail": f"{name} answered", "latency_ms": 1.0}
        for name in ("postgres", "redis", "tool_server")
    }
    found["langsmith"] = {"status": "off", "detail": "no LANGCHAIN_API_KEY"}
    found.update(dependencies)
    return {"http://api/admin/health-deep": (http, {"status": status, "took_ms": 5.0, "dependencies": found})}


def test_the_check_reads_the_deep_check_and_that_it_is_closed_without_the_token(check, monkeypatch):
    asked = []
    answers = _deep("ok")

    def get(url, headers=None, **kwargs):
        asked.append(headers or {})
        status, body = answers[url]
        return httpx.Response(
            status if headers else 404, json=body if headers else {}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(check.httpx, "get", get)
    checks = check.Checks()
    check.the_deep_check_answers(checks, _where(check))
    assert (checks.passed, checks.failed) == (6, 0)
    assert asked == [{"Authorization": "Bearer tok"}, {}]


@pytest.mark.parametrize(
    "answers",
    [
        _deep("failing", 503, redis={"status": "failing", "detail": "ConnectionError"}),
        _deep("degraded", langsmith={"status": "failing", "detail": "the key was refused (HTTP 403)"}),
    ],
)
def test_the_check_says_no_to_a_dependency_failing(check, monkeypatch, answers):
    monkeypatch.setattr(check.httpx, "get", _answers(answers))
    checks = check.Checks()
    check.the_deep_check_answers(checks, _where(check, ops_token=""))
    assert checks.failed >= 1


def test_the_check_script_asks_sections_8_and_9_after_the_dashboard(check, monkeypatch):
    from unittest.mock import MagicMock

    called = []
    for name in ("the_dashboard_shows_it", "the_alerts_are_watched", "the_deep_check_answers"):
        monkeypatch.setattr(check, name, MagicMock(side_effect=lambda *a, name=name, **k: called.append(name)))
    monkeypatch.setattr(check, "the_stack_answers", lambda *_: True)
    monkeypatch.setattr(check, "the_backend_shows_its_metrics", lambda *_: "host.docker.internal:8200")
    monkeypatch.setattr(sys, "argv", ["observability_check.py", "--skip-plan"])
    check.main()
    assert called == ["the_dashboard_shows_it", "the_alerts_are_watched", "the_deep_check_answers"]

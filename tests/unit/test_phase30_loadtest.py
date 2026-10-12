"""Phase 30 — the load test's script, and the program that runs it and watches the stack meanwhile.

Neither is the app, and both decide what the report says: the k6 script what a "visit" is, and
scripts/loadtest.py what a run's figures are and whether a rate "holds". A k6 script cannot be run
here, so it is read: its endpoints are held to the app's own routes, its polling to the page's,
and every request it makes is checked for the tag that keeps an event stream's lifetime out of the
latency figures. The runner's arithmetic is run on what the stack really answers — PgBouncer's
SHOW POOLS, pg_stat_activity, GET /metrics, docker stats, k6's summary.
"""

import json
import re
import sys
from importlib import util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from app.main import app

ROOT = Path(__file__).resolve().parents[2]
K6 = (ROOT / "loadtest/k6/traveller.js").read_text()
OVERLAY_TEXT = (ROOT / "docker/loadtest/compose.yml").read_text()
OVERLAY = yaml.safe_load(OVERLAY_TEXT.replace("!reset null", "null"))


def script(name: str):
    """A file from scripts/ as a module — they are programs, not a package.

    Under a name of its own: scripts/loadtest.py shares its name with the `loadtest` package, and
    registered as "loadtest" it would be what every later `import loadtest.standins` finds.
    """
    spec = util.spec_from_file_location(f"scripts_{name}", ROOT / "scripts" / f"{name}.py")
    module = util.module_from_spec(spec)
    sys.modules[f"scripts_{name}"] = module
    spec.loader.exec_module(module)
    return module


runner = script("loadtest")


# ── The k6 script: what a visit is ─────────────────────────────────────────


def endpoints() -> dict[str, str]:
    block = re.search(r"const ENDPOINTS = \{(.*?)\};", K6, flags=re.S).group(1)
    return dict(re.findall(r"(\w+): '([^']+)'", block))


def test_every_endpoint_the_script_asks_is_a_route_the_app_has():
    routes = {(method.upper(), path) for path, item in app.openapi()["paths"].items() for method in item}
    asked = endpoints()

    for name in asked.values():
        method, path = name.split(" ", 1)
        assert (method, path.replace("{id}", "{trip_id}")) in routes, f"{name} is not a route of this app"
    # the roadmap's four, and what the trip page asks around them
    for wanted in ("POST /trips", "POST /trips/{id}/plan", "GET /trips/{id}/stream", "GET /trips/{id}/itinerary"):
        assert wanted in asked.values(), wanted
    assert {"POST /auth/login", "GET /trips", "GET /trips/{id}", "GET /trips/{id}/status"} <= set(asked.values())


def test_visits_start_at_a_fixed_rate_whatever_the_server_does():
    """An open model. A fixed number of users each waiting for an answer slows down with the server — and hides it."""
    assert "executor: 'constant-arrival-rate'" in K6
    assert (
        "rate: perMinute" in K6 and "timeUnit: '1m'" in K6
    )  # k6's rates are whole numbers: a quarter a second is 15 a minute
    assert "maxVUs:" in K6 and "preAllocatedVUs:" in K6
    assert "visitsStarted.add(1)" in K6  # what was offered is counted, not assumed


def test_an_event_streams_lifetime_is_kept_out_of_the_latency_figures():
    """The stream extension reports a stream's whole life as an HTTP request's duration: tagged apart, or it is the p99."""
    (opened,) = re.findall(r"sse\.open\((.*?)function \(client\)", K6, flags=re.S)
    assert "kind: 'stream'" in opened and "name: ENDPOINTS.stream" in opened
    assert "'http_req_duration{kind:api}'" in K6 and "'http_req_duration{kind:stream}'" in K6

    # every request the script makes itself says which kind it is
    made = [found.start() for found in re.finditer(r"http\.(?:request|post|get)\(", K6)]
    assert len(made) == 2  # one helper for the API, one for the login form
    for at in made:
        assert re.search(r"tags: \{ kind: 'api', name", K6[at : at + 400]), K6[at : at + 80]
    (together,) = re.findall(r"http\.batch\(\[(.*?)\]\);", K6, flags=re.S)
    assert together.count("kind: 'api'") == together.count("['GET'") == 2
    # and a stream's address — the trip's id and the login token — is not a tag: that would be a series each
    (tags,) = re.findall(r"systemTags: \[(.*?)\]", K6)
    assert "'url'" not in tags and "'name'" in tags and "'status'" in tags


def test_the_stream_is_opened_as_a_browser_opens_it_and_read_as_this_server_writes_it():
    assert "/stream?token=${encodeURIComponent(token)}" in K6  # an EventSource cannot send a header
    assert "String(event.name || '').trim()" in K6  # this server ends its lines \r\n; the extension leaves the \r on
    for said in ("'connected'", "'agent_update'", "'builder_token'", "'planning_complete'", "'planning_failed'"):
        assert said in K6, said
    assert (
        "timeout: `${WRITES_FOR[1] + GIVE_UP_AFTER}s`" in K6
    )  # a traveller's patience: no stream is waited on for ever


def test_a_plans_duration_is_measured_from_the_request_to_the_event():
    """POST /plan answers 202 in milliseconds: its own duration says nothing about how long a plan takes."""
    assert "new Trend('plan_duration', true)" in K6
    asked = K6.index("asked = Date.now();")
    assert asked < K6.index("ENDPOINTS.plan, token, { raw_input") < K6.index("planDuration.add(Date.now() - asked)")
    assert "answer.status !== 202" in K6  # a plan that was not started is a failed plan, not a fast one
    for why in ("refused", "failed", "gave_up", "no_stream"):  # and every way of not getting a plan has a name
        assert f"'{why}'" in K6, why


def test_the_script_asks_for_the_status_as_often_as_the_page_does():
    page = (ROOT / "src/frontend/src/app/trips/[id]/page.tsx").read_text()
    every = int(re.search(r"const POLL_MS = ([\d_]+);", page).group(1).replace("_", ""))

    assert f"const ASK_STATUS_EVERY_MS = {every};" in K6
    # no timer fires while a stream is open: it catches up when an event arrives
    assert "Math.floor((Date.now() - asked) / ASK_STATUS_EVERY_MS)" in K6 and "setInterval" not in K6


def test_the_accounts_are_made_before_the_load_and_not_by_the_script():
    """Making an account hashes a password — a fifth of a second of the server doing nothing else."""
    assert "/auth/register" not in K6 and "export function setup" not in K6
    assert "new SharedArray('accounts', () => JSON.parse(open(__ENV.ACCOUNTS)))" in K6
    assert runner.accounts_for(rate=1.0, hold=180) == 51  # one for every three or four visits, whatever the rate
    assert runner.accounts_for(rate=0.1, hold=180) == 5 == runner.accounts_for(rate=0.01, hold=10)


def test_half_the_visits_sign_in():
    """A login lasts a day: someone who comes back still has theirs. And a sign-in is the dearest request there is —
    a fifth of a second of the whole process — so how many of them there are decides what the load costs."""
    assert "const SIGN_IN_SHARE = parseFloat(__ENV.SIGN_IN_SHARE || '0.5');" in K6
    assert "if (Math.random() < SIGN_IN_SHARE) token = signIn(account) || token;" in K6


def test_the_script_and_the_runner_agree_on_how_long_a_traveller_waits():
    assert f"__ENV.GIVE_UP_AFTER || '{runner.GIVE_UP_AFTER_S}'" in K6


# ── The runner: the stack it starts ────────────────────────────────────────


def test_what_it_builds_is_pinned():
    assert re.fullmatch(r"v2\.\d+\.\d+", runner.K6_VERSION)
    assert re.fullmatch(r"github\.com/phymbert/xk6-sse@v\d+\.\d+\.\d+", runner.SSE_EXTENSION)
    assert re.fullmatch(r"grafana/xk6:\d+\.\d+\.\d+", runner.XK6_IMAGE)  # never :latest
    ignored = (ROOT / ".gitignore").read_text()
    assert ".loadtest/" in ignored and runner.WORK == ROOT / ".loadtest"  # the binary, the logs, the accounts' tokens


def test_it_starts_the_production_like_stack_with_the_stand_ins_laid_over_it():
    assert [path.relative_to(ROOT).as_posix() for path in runner.COMPOSE_FILES] == [
        "docker/prodlike/compose.yml",
        "docker/loadtest/compose.yml",
    ]
    backend = OVERLAY["services"]["backend"]
    assert backend["image"] == runner.LOADTEST_IMAGE and "build: !reset null" in OVERLAY_TEXT
    assert list(OVERLAY["services"]) == ["backend"]  # the database, the pooler and the Redis are that stack's own
    # the image is the deployed one with a directory added — built in that order, the first under the name the second is FROM
    assert runner.DEPLOYED_IMAGE in (ROOT / "docker/loadtest/Dockerfile").read_text()
    said = re.search(r'^SAYS = "(.+?)"', (ROOT / "loadtest/standins/app.py").read_text(), flags=re.M).group(1)
    assert runner.STAND_INS_SAY == said


def test_a_stack_whose_log_does_not_say_stand_ins_is_not_called_ready(capsys):
    """Built, started and answering — and the wrong image: `up` says so and does not say "ready"."""
    prodlike = MagicMock()
    prodlike.wait_until_ready.return_value = True
    with (
        patch.object(runner, "_script", return_value=prodlike),
        patch.object(runner, "run", return_value=MagicMock(returncode=0)),
        patch.object(runner, "compose", return_value=0),
        patch.object(runner, "commit", return_value="33e3f94"),
        patch.object(runner, "stand_ins_are_in", return_value=False),
    ):
        assert runner.up() == 1

    said = capsys.readouterr().out
    assert "this is not the load test's image" in said and "ready:" not in said
    prodlike.hand_over_the_token.assert_not_called()  # nothing further is done with it


def test_the_small_pools_are_the_baselines(tmp_path):
    """--small-pools: the connections the stack had when it could not make a plan every five seconds."""
    from app.core.config import Settings

    small = yaml.safe_load((ROOT / "docker/loadtest/small-pools.yml").read_text())
    # the app's pool as it was before it was a setting — which is what the settings' defaults still are
    assert small["services"]["backend"]["environment"] == {
        "DB_POOL_SIZE": str(Settings.model_fields["DB_POOL_SIZE"].default),
        "DB_MAX_OVERFLOW": str(Settings.model_fields["DB_MAX_OVERFLOW"].default),
    }
    assert runner.SMALL_POOLER == ROOT / ".loadtest" / "pgbouncer-small-pools.ini"
    assert small["services"]["pooler"]["volumes"] == [
        "../../.loadtest/pgbouncer-small-pools.ini:/etc/pgbouncer/pgbouncer.ini:ro"
    ]

    with patch.object(runner, "SMALL_POOLER", tmp_path / "pgbouncer-small-pools.ini"):
        written = runner.small_pooler()
    was, now = (ROOT / "docker/prodlike/pgbouncer.ini").read_text().splitlines(), written.read_text().splitlines()
    # the pooler's settings with one number changed, and nothing else
    assert [(a, b) for a, b in zip(was, now, strict=True) if a != b] == [
        ("default_pool_size = 90", "default_pool_size = 4")
    ]
    assert runner.BASELINE_POOLER_CONNECTIONS == 4


def test_it_puts_no_load_on_a_stack_without_the_stand_ins(capsys):
    """The production-like stack with real keys in it looks the same from outside — and cannot take this."""
    with patch.object(runner, "stand_ins_are_in", return_value=False), patch.object(runner, "run_k6") as k6:
        assert runner.a_run("x", rate=1.0, hold=10) is None

    k6.assert_not_called()
    assert "the real providers cannot take this" in capsys.readouterr().out


def test_one_request_in_ten_is_traced_while_the_load_runs():
    assert OVERLAY["services"]["backend"]["environment"]["OTEL_TRACES_SAMPLE_RATIO"] == "${LOADTEST_TRACE_RATIO:-0.1}"


def test_the_pooler_can_be_asked_how_it_is_doing():
    pooler = (ROOT / "docker/prodlike/pgbouncer.ini").read_text()
    assert re.search(r"^stats_users = tripplanner$", pooler, flags=re.M)
    assert "admin_users" not in pooler  # to look, not to change anything
    assert "dbname=pgbouncer" in " ".join(runner.ASK_POOLER) and runner.ASK_POOLER[-1] == "SHOW POOLS"


# ── The runner: a machine that fell asleep ─────────────────────────────────


def test_a_machine_that_slept_is_told_from_one_that_did_not():
    wall, steady = [1_000.0], [50.0]
    with patch.object(runner.time, "time", lambda: wall[0]), patch.object(runner.time, "monotonic", lambda: steady[0]):
        awake = runner.Awake()
        wall[0] += 120.0
        steady[0] += 120.0  # two minutes went by on both clocks
        assert not awake.slept() and awake.asleep_for() == 0.0
        wall[0] += 34.0  # …and then 34 seconds on the wall that no program saw
        assert awake.slept() and awake.asleep_for() == pytest.approx(34.0)


def test_a_run_the_machine_slept_through_is_not_kept(tmp_path, capsys):
    class Slept:
        def slept(self) -> bool:
            return True

        def asleep_for(self) -> float:
            return 14.0

    with (
        patch.object(runner, "stand_ins_are_in", return_value=True),
        patch.object(runner, "Awake", Slept),
        patch.object(runner, "make_accounts", return_value=tmp_path / "accounts.json"),
        patch.object(runner, "kept_awake", return_value=None),
        patch.object(runner, "RESULTS", tmp_path),
        patch.object(runner, "run_k6") as k6,
    ):
        assert runner.a_run("baseline", rate=0.5, hold=10) is None

    k6.assert_not_called()  # it had already slept while the accounts were made: nothing was started
    assert not list(tmp_path.rglob("*.json"))
    assert "fell asleep for 14 s" in capsys.readouterr().out


def test_a_run_slept_through_while_the_load_was_on_is_not_kept_either(tmp_path, capsys):
    """Awake while the accounts were made, asleep for a while under load: k6 finished, its figures exist — and are wrong."""

    class SleptLater:
        looks = 0

        def slept(self) -> bool:
            SleptLater.looks += 1
            return SleptLater.looks > 1  # not at the first look, before the load; at the one after it

        def asleep_for(self) -> float:
            return 14.0

    class NothingToWatch:
        samples, containers, _began = [{}], [], 0.0

        def __init__(self, *_) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def quiet(self) -> bool:
            return True

    def k6_ran(rate, hold, summary_path, accounts, options=None) -> int:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary()))
        return 0

    with (
        patch.object(runner, "stand_ins_are_in", return_value=True),
        patch.object(runner, "Awake", SleptLater),
        patch.object(runner, "make_accounts", return_value=tmp_path / "accounts.json"),
        patch.object(runner, "kept_awake", return_value=None),
        patch.object(runner, "ops_token", return_value="a-token"),
        patch.object(runner, "Watch", NothingToWatch),
        patch.object(runner, "RESULTS", tmp_path / "results"),
        patch.object(runner, "WORK", tmp_path / "work"),
        patch.object(runner, "run_k6", side_effect=k6_ran) as k6,
    ):
        assert runner.a_run("baseline", rate=0.5, hold=10) is None

    k6.assert_called_once()
    assert list((tmp_path / "work").rglob("*.json"))  # k6 did leave its summary…
    assert not list((tmp_path / "results").rglob("*.json"))  # …and no result was made of it
    assert "was asleep for 14 s of the run" in capsys.readouterr().out


def test_a_ladder_stops_at_the_rate_where_the_stack_fell_over(capsys):
    """Fewer than half the visits got a plan: a higher rate would only take longer to say the same."""

    def a_run(name, rate, hold, options=None) -> dict:
        return {"k6": {"plans_arrived_of_offered": 1.0 if rate < 0.8 else 0.35}}

    with patch.object(runner, "a_run", side_effect=a_run) as ran, patch.object(runner, "table"):
        assert runner.ladder("x", [0.4, 0.8, 1.6], hold=10, keep_going=False) == 0
    assert [call.args[1] for call in ran.call_args_list] == [0.4, 0.8]
    assert "higher rates are not tried" in capsys.readouterr().out

    with patch.object(runner, "a_run", side_effect=a_run) as ran, patch.object(runner, "table"):
        runner.ladder("x", [0.4, 0.8, 1.6], hold=10, keep_going=True)
    assert [call.args[1] for call in ran.call_args_list] == [0.4, 0.8, 1.6]  # …unless it is asked to


# ── The runner: reading the stack ──────────────────────────────────────────

SHOW_POOLS = """database|user|cl_active|cl_waiting|cl_active_cancel_req|cl_waiting_cancel_req|sv_active|sv_active_cancel|sv_being_canceled|sv_idle|sv_used|sv_tested|sv_login|maxwait|maxwait_us|pool_mode
pgbouncer|pgbouncer|1|0|0|0|0|0|0|0|0|0|0|0|0|statement
tripplanner|tripplanner|4|11|0|0|4|0|0|0|0|0|0|7|250000|transaction
(2 rows)
"""


def test_the_poolers_own_account_of_who_is_waiting():
    pool = runner.parse_pools(SHOW_POOLS)

    assert pool["cl_active"] == 4 and pool["cl_waiting"] == 11  # four being served, eleven waiting for a connection
    assert pool["sv_active"] == 4 and pool["maxwait"] == 7  # …the oldest of them for seven seconds
    assert "pool_mode" not in pool and "database" not in pool  # numbers only
    assert runner.parse_pools("") == {} == runner.parse_pools("psql: error: connection refused")


def test_what_postgres_says_its_connections_are_doing():
    states = runner.parse_activity("idle|1|0\nidle in transaction|3|4.8\nactive|1|0.0\n")

    assert states["idle in transaction"] == {"n": 3, "oldest_s": 4.8}
    assert states["active"]["n"] == 1 and runner.parse_activity("") == {}


METRICS = """# HELP planning_runs_in_progress Planning runs this process is making right now.
# TYPE planning_runs_in_progress gauge
planning_runs_in_progress 7.0
# TYPE http_requests_inprogress gauge
http_requests_inprogress{handler="/trips/{trip_id}/stream",method="GET"} 23.0
http_requests_inprogress{handler="/trips/{trip_id}/status",method="GET"} 2.0
http_requests_inprogress{handler="/trips",method="POST"} 1.0
# TYPE db_pool_connections gauge
db_pool_connections{state="in_use"} 15.0
db_pool_connections{state="idle"} 0.0
# TYPE db_pool_connections_limit gauge
db_pool_connections_limit 15.0
# TYPE redis_pool_connections gauge
redis_pool_connections{state="in_use"} 24.0
redis_pool_connections{state="idle"} 3.0
# TYPE redis_pool_connections_limit gauge
redis_pool_connections_limit 100.0
# TYPE event_loop_lag_seconds histogram
event_loop_lag_seconds_bucket{le="0.01"} 90.0
event_loop_lag_seconds_bucket{le="0.25"} 99.0
event_loop_lag_seconds_bucket{le="+Inf"} 100.0
event_loop_lag_seconds_count 100.0
# TYPE process_cpu_seconds_total counter
process_cpu_seconds_total 41.5
# TYPE process_resident_memory_bytes gauge
process_resident_memory_bytes 2.5e+08
# TYPE planning_run_duration_seconds histogram
planning_run_duration_seconds_count{kind="plan",outcome="completed"} 40.0
planning_run_duration_seconds_count{kind="plan",outcome="crashed"} 2.0
# TYPE http_requests_total counter
http_requests_total{handler="/trips",method="GET",status="2xx"} 300.0
http_requests_total{handler="/trips",method="GET",status="5xx"} 12.0
# TYPE mcp_cache_lookups_total counter
mcp_cache_lookups_total{cache="flights",result="hit"} 30.0
mcp_cache_lookups_total{cache="hotels",result="hit"} 28.0
mcp_cache_lookups_total{cache="flights",result="miss"} 9.0
"""


def test_the_backends_own_numbers_as_a_load_test_reads_them():
    seen = runner.parse_metrics(METRICS)

    assert seen["runs_in_progress"] == 7 and seen["streams_open"] == 23 and seen["requests_in_progress"] == 3
    assert (seen["db_in_use"], seen["db_idle"], seen["db_limit"]) == (15, 0, 15)
    assert (seen["redis_in_use"], seen["redis_limit"]) == (24, 100)
    assert seen["cpu_seconds"] == 41.5 and seen["memory_mb"] == 250.0
    assert seen["runs"] == {"completed": 40, "crashed": 2} and seen["requests"] == {"2xx": 300, "5xx": 12}
    assert seen["cache"] == {"hit": 58, "miss": 9}
    assert seen["lag_buckets"] == {"0.01": 90, "0.25": 99, "+Inf": 100}


def test_every_number_the_runner_reads_is_one_the_app_exports():
    """A renamed metric would leave a column of the report quietly empty."""
    from prometheus_client import REGISTRY, generate_latest

    exported = generate_latest(REGISTRY).decode()
    source = (ROOT / "scripts/loadtest.py").read_text()
    reading = source[source.index("def parse_metrics") : source.index("def parse_docker_stats")]
    read = set(re.findall(r'name == "([a-z_]+)"', reading)) | set(
        re.findall(r'"([a-z_]+_connections(?:_limit)?)"', reading)
    )
    assert len(read) >= 10
    # the process's own CPU and memory are the client library's, and it only has them where there is a /proc: Linux
    for name in read - {"process_cpu_seconds_total", "process_resident_memory_bytes"}:
        assert re.search(
            rf"^# TYPE {name.removesuffix('_bucket').removesuffix('_count').removesuffix('_total')}",
            exported,
            flags=re.M,
        ), name
    assert f'handler="{runner.STREAM}"' in METRICS and runner.STREAM in app.openapi()["paths"]


def test_a_histograms_quantile_between_two_readings():
    before = {"0.01": 100.0, "0.25": 100.0, "+Inf": 100.0}
    after = {"0.01": 190.0, "0.25": 199.0, "+Inf": 200.0}  # a hundred more: ninety quick, nine late, one very late

    assert runner.quantile_from_buckets(before, after, 0.5) == 0.01
    assert runner.quantile_from_buckets(before, after, 0.99) == 0.25
    assert runner.quantile_from_buckets(before, after, 1.0) == float("inf")
    assert runner.quantile_from_buckets(after, after, 0.99) is None  # nothing was observed in between


def test_what_each_container_uses():
    shown = (
        "tripplanner-prodlike-backend-1|187.52%|412.3MiB / 7.75GiB\n"
        "tripplanner-prodlike-neon-standin-1|35.10%|1.21GiB / 7.75GiB\n"
        "tripplanner-observability-jaeger-1|2.00%|88MiB / 7.75GiB\n"
        "tripplanner_redis|0.30%|12.2MiB / 7.75GiB\n"
    )
    seen = runner.parse_docker_stats(shown)

    assert seen["backend"] == {"cpu": 187.52, "mb": 432.3} and seen["neon-standin"]["mb"] == 1299.2
    assert set(seen) == {"backend", "neon-standin", "jaeger", "redis"}


def look(t: float, plans: int, waiting: int, in_txn: int, cpu: float, lag: dict) -> dict:
    return {
        "t": t,
        "pooler": {"cl_active": 4, "cl_waiting": waiting, "sv_active": 4, "maxwait": waiting // 2},
        "postgres": {"idle in transaction": {"n": in_txn, "oldest_s": 6.5}},
        "app": {
            "runs_in_progress": plans,
            "streams_open": plans * 3,
            "db_in_use": min(15, plans + 2),
            "db_limit": 15,
            "redis_in_use": plans * 3,
            "redis_limit": 100,
            "cpu_seconds": cpu,
            "memory_mb": 240.0 + plans,
            "lag_buckets": lag,
            "runs": {"completed": plans * 2},
            "requests": {"2xx": plans * 30, "5xx": plans},
            "cache": {"hit": plans * 6},
        },
        "metrics_took_ms": 20 + plans,
    }


def test_what_the_stack_did_while_the_load_was_on():
    samples = [
        look(0.0, 0, 0, 0, 10.0, {"0.01": 0, "+Inf": 0}),
        look(2.0, 6, 3, 4, 11.0, {"0.01": 18, "+Inf": 20}),
        look(4.0, 14, 11, 4, 12.4, {"0.01": 30, "+Inf": 40}),
        {"t": 6.0, "pooler": {}, "postgres": {}, "app": {}, "metrics_took_ms": 8000},  # the backend did not answer
        look(60.0, 0, 0, 0, 30.0, {"0.01": 600, "+Inf": 640}),  # afterwards: not part of the load
    ]
    containers = [{"t": 3.0, "backend": {"cpu": 120.0, "mb": 300.0}, "neon-standin": {"cpu": 40.0, "mb": 900.0}}]

    told = runner.summarise(samples, containers, until=10.0)

    assert told["samples"] == 4 and told["metrics_unanswered"] == 1 and told["metrics_took_ms_max"] == 8000
    assert told["plans_in_progress"] == {"max": 14, "mean": 6.67}
    assert told["db_pool"] == {"in_use_max": 15, "in_use_mean": 8.33, "limit": 15}
    assert told["pooler"]["clients_waiting_max"] == 11 and told["pooler"]["longest_wait_s"] == 5
    assert told["postgres"]["idle_in_transaction_max"] == 4 and told["postgres"]["oldest_open_transaction_s"] == 6.5
    assert told["backend_cpu_cores"] == pytest.approx(
        (12.4 - 10.0) / 6.0, abs=0.01
    )  # between the first look and the last
    # past the histogram's last edge it is "more than" that edge — never infinity, which no report can say
    assert told["event_loop_lag_s"] == {"p50": 0.01, "p99": ">0.01", "max": ">0.01"}
    assert runner.late(">2.5") == "over 2500 ms" and runner.late(0.025) == "25 ms" and runner.late(None) == "—"
    json.dumps(told, allow_nan=False)  # what is kept of a run is JSON any program reads
    assert told["runs"] == {"completed": 28} and told["requests"]["5xx"] == 14
    assert (
        told["containers"]["backend"] == {"cpu_max": 120.0, "mb_max": 300.0} and told["docker_memory_mb_max"] == 1200.0
    )
    # and what is kept as a time series is one short line a look
    series = runner.compact(samples)
    assert series[2] == {
        "t": 4.0,
        "plans": 14,
        "streams": 42,
        "db_in_use": 15,
        "redis_in_use": 42,
        "pooler_serving": 4,
        "pooler_waiting": 11,
        "pooler_longest_wait_s": 5,
        "idle_in_transaction": 4,
        "metrics_took_ms": 34,
    }
    assert series[3]["plans"] is None  # a look the backend did not answer is a gap, not a zero


# ── The runner: a run's figures, and whether a rate holds ──────────────────


def trend(p50: float, p95: float, p99: float, worst: float, count: int) -> dict:
    return {"med": p50, "p(95)": p95, "p(99)": p99, "max": worst, "count": count, "avg": p50, "min": 1.0, "p(90)": p95}


def summary(**changed) -> dict:
    metrics = {
        "visits_started": {"count": 90},
        "plans_asked": {"count": 90},
        "plans_completed": {"count": 90},
        "plans_failed": {"count": 0},
        "plan_duration": trend(11_800, 14_900, 16_100, 16_400, 90),
        "plan_first_piece": trend(9_900, 12_500, 13_000, 13_300, 90),
        "stream_opened": trend(8, 20, 40, 60, 90),
        "http_reqs{kind:api}": {"count": 1_260, "rate": 5.1},
        "http_req_failed{kind:api}": {"rate": 0.0},
        "http_req_duration{kind:api}": trend(9, 190, 240, 410, 1_260),
        "http_req_duration{kind:stream}": trend(41_000, 58_000, 60_000, 61_000, 90),
        "http_req_duration{name:GET /trips/{id}/status}": trend(7, 20, 33, 90, 540),
        "http_req_failed{name:GET /trips/{id}/status}": {"rate": 0.0},
        "http_req_duration{name:POST /auth/login}": trend(195, 230, 260, 300, 45),
        "http_req_failed{name:POST /auth/login}": {"rate": 0.0},
        "vus_max": {"value": 35, "min": 35, "max": 35},
    }
    metrics.update(changed)
    return {"rate": 0.5, "hold": 180, "metrics": metrics}


def test_a_runs_figures():
    k6 = runner.figures(summary())

    assert (k6["visits_offered"], k6["plans_completed"], k6["plans_arrived_of_offered"]) == (90, 90, 1.0)
    assert k6["plans_per_second"] == 0.5
    assert k6["plan_ms"] == {"p50": 11_800, "p95": 14_900, "p99": 16_100, "max": 16_400, "count": 90}
    # the latency is of the requests that are not streams: a stream's 41 seconds is how long the page was open
    assert k6["api"]["p99"] == 240 and k6["api"]["per_second"] == 7.0 and k6["api"]["failed"] == 0.0
    assert k6["endpoints"]["GET /trips/{id}/status"]["p99"] == 33 and k6["endpoints"]["POST /auth/login"]["p50"] == 195
    assert runner.judge(k6) == []  # nothing against it: this rate holds


def test_plans_a_second_are_the_plans_that_arrived():
    k6 = runner.figures(summary(plans_completed={"count": 45}))  # ninety asked for, half of them made

    assert k6["plans_asked"] == 90 and k6["plans_per_second"] == 0.25
    assert runner.judge(k6) == ["45 of 90 visits got a plan"]


def test_a_visit_that_could_not_even_start_was_offered_all_the_same():
    k6 = runner.figures(summary(visits_started={"count": 300}, dropped_iterations={"count": 60}))

    assert k6["visits_offered"] == 360 and k6["plans_arrived_of_offered"] == 0.25
    assert "60 visits could not even start" in runner.judge(k6)


@pytest.mark.parametrize(
    ("changed", "why"),
    [
        ({"plans_completed": {"count": 80}}, "80 of 90 visits got a plan"),
        ({"plan_duration": trend(21_000, 47_000, 60_000, 90_000, 90)}, "a plan took 47 s at p95"),
        ({"http_req_duration{kind:api}": trend(15, 900, 4_300, 30_000, 1_260)}, "requests took 4300 ms at p99"),
        ({"http_req_failed{kind:api}": {"rate": 0.034}}, "3.4% of requests failed"),
    ],
)
def test_a_rate_does_not_hold_when_a_traveller_would_say_so(changed, why):
    assert runner.judge(runner.figures(summary(**changed))) == [why]


def test_what_holding_means_is_written_down():
    assert (runner.PLANS_MUST_ARRIVE, runner.PLAN_P95_S, runner.API_P99_MS, runner.API_ERRORS) == (
        0.99,
        30.0,
        1000.0,
        0.01,
    )


def test_the_runs_kept_are_read_back_as_a_table(tmp_path, capsys):
    k6 = runner.figures(summary())
    stack = runner.summarise(
        [look(0.0, 2, 0, 1, 1.0, {"0.01": 0, "+Inf": 0}), look(2.0, 6, 3, 4, 2.0, {"0.01": 9, "+Inf": 10})], [], 9.0
    )
    for rate, holds in ((0.5, True), (0.25, True), (1.0, False)):
        kept = {
            "name": "baseline",
            "rate": rate,
            "hold_s": 180,
            "holds": holds,
            "why_not": [],
            "k6": k6,
            "stack": stack,
        }
        (tmp_path / "baseline").mkdir(exist_ok=True)
        (tmp_path / "baseline" / f"rate-{rate:g}.json").write_text(json.dumps(kept))

    with patch.object(runner, "RESULTS", tmp_path):
        assert [kept["rate"] for kept in runner.runs_of("baseline")] == [0.25, 0.5, 1.0]  # lowest rate first
        runner.table(["baseline", "nothing-by-this-name"])

    shown = capsys.readouterr().out
    assert "| 0.5 | 90 of 90 | 0.50 | 11.8 s / 14.9 s | 7.0 | 9 ms / 190 ms / 240 ms | 0.0% | yes |" in shown
    assert "| 1 | 90 of 90 |" in shown and shown.count("| no |") == 1
    assert "| 0.5 | 6 | 18 | 8 of 15 | 4 / 3 | 1 s | 4 | over 10 ms | 0.5 cores |" in shown
    assert "no runs kept as 'nothing-by-this-name'" in shown

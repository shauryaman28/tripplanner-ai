"""The load test (Phase 30): the stack it runs against, the load, and what the stack did under it.

    python scripts/loadtest.py k6                      # build the k6 that can read an event stream (once; needs Docker)
    python scripts/loadtest.py up                      # the production-like stack, with stand-ins for models and providers
    python scripts/loadtest.py up --small-pools        # …with the baseline's 4 + 15 database connections put back
    python scripts/loadtest.py smoke                   # one traveller, three visits: does every part work
    python scripts/loadtest.py one-plan                # one plan, timed step by step (the stand-ins' — or a real one's)
    python scripts/loadtest.py run --rate 0.5          # visits started at 0.5 a second, for --hold seconds
    python scripts/loadtest.py ladder --name baseline  # one rate after another: where does it stop coping
    python scripts/loadtest.py table baseline after    # the runs kept in docs/loadtest/, as tables
    python scripts/loadtest.py down

What is under load is the image that is deployed, on the production-like stack of Phase 27
(docker/prodlike/) — a TLS-only Postgres behind a pooler in transaction mode, a TLS-only Redis —
with one directory added: stand-ins for the models and the providers, which cannot take load and
each wait as long as the real thing takes (loadtest/standins/). The load is loadtest/k6/traveller.js.

While k6 runs, this script looks at the stack every two seconds and keeps what it sees beside
k6's own figures (docs/loadtest/<name>/rate-<r>.json):

    the pooler      SHOW POOLS — clients being served, clients WAITING for a server connection
    Postgres        pg_stat_activity — how many connections are idle inside a transaction
    the backend     GET /metrics — plans being made, streams open, the database and Redis pools,
                    how late the event loop runs, the CPU the process used
    Docker          docker stats — each container's memory, against what Docker Desktop has

Watch it live in Grafana meanwhile: python scripts/observability.py up → http://localhost:3001.

One machine runs both the load and the stack. The figures say where this app runs out first and
what a change does to that; they are not what a deployment will do.

And that machine has to stay awake. A laptop on battery with its lid closed sleeps for seconds at a
time while everything on it appears to keep running: the stack freezes, k6 freezes, and a run
comes out slower than it was without anything looking wrong. A run during which the machine slept
is not kept (`Awake`).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import secrets
import statistics
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
API = "http://localhost:8200"
RESULTS = ROOT / "docs" / "loadtest"
SCRIPT = ROOT / "loadtest" / "k6" / "traveller.js"
WORK = ROOT / ".loadtest"  # git-ignored: the k6 binary, k6's own logs
K6 = WORK / "bin" / "k6-sse"

# The k6 that is built: k6 itself has no event streams (loadtest/k6/traveller.js says what was tried).
K6_VERSION, SSE_EXTENSION, XK6_IMAGE = "v2.3.0", "github.com/phymbert/xk6-sse@v0.2.0", "grafana/xk6:1.4.14"

DEPLOYED_IMAGE, LOADTEST_IMAGE = "tripplanner-backend:under-test", "tripplanner-loadtest-backend"
COMPOSE_FILES = (ROOT / "docker" / "prodlike" / "compose.yml", ROOT / "docker" / "loadtest" / "compose.yml")
# The baseline's connections put back, to see what a change to the code does without more of them.
SMALL_POOLS = ROOT / "docker" / "loadtest" / "small-pools.yml"
SMALL_POOLER, BASELINE_POOLER_CONNECTIONS = WORK / "pgbouncer-small-pools.ini", 4
POOLER_SETTINGS = ROOT / "docker" / "prodlike" / "pgbouncer.ini"
SECRETS = ROOT / ".env.prodlike"
BACKEND, POSTGRES = "tripplanner-prodlike-backend-1", "tripplanner-prodlike-neon-standin-1"
STAND_INS_SAY = "[STAND-INS]"  # what the backend logs as it starts with them in (loadtest/standins/app.py)

SAMPLE_EVERY_S = 2.0
STREAM = "/trips/{trip_id}/stream"

# When a rate "holds": what a traveller would call working. Past any of these it does not.
GIVE_UP_AFTER_S = 180  # how long a traveller waits for a plan (the k6 script's GIVE_UP_AFTER)
PLANS_MUST_ARRIVE = 0.99  # of the plans asked for
PLAN_P95_S = 30.0  # a plan takes 10 to 16 s when nothing is in its way
API_P99_MS = 1000.0  # every request that is not a stream
API_ERRORS = 0.01


class Awake:
    """Whether this machine has been awake the whole time since this was made.

    The clock on the wall goes on while a machine sleeps; the steady clock a program times things by
    stops. So the two drift apart by exactly as long as the machine was asleep — which is how a run
    that was slept through is told from one that was not. Found the hard way: the first smoke run
    of this phase had a 34-second hole in it, and the power log said why.
    """

    A_NAP_S = 1.0  # less than this is the two clocks being read a moment apart

    def __init__(self) -> None:
        self._wall, self._steady = time.time(), time.monotonic()

    def asleep_for(self) -> float:
        return max(0.0, (time.time() - self._wall) - (time.monotonic() - self._steady))

    def slept(self) -> bool:
        return self.asleep_for() >= self.A_NAP_S


def kept_awake(seconds: float) -> subprocess.Popen | None:
    """On a Mac, ask it not to fall asleep for this long (caffeinate). It helps with the lid open —
    not with the lid closed on battery, which nothing a program can do prevents."""
    if platform.system() != "Darwin":
        return None
    try:
        return subprocess.Popen(["caffeinate", "-dimsu", "-t", str(int(seconds))])
    except OSError:
        return None


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shown(path: Path) -> str:
    """A path as it is said to a person: from the repository's root when it is inside it."""
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def run(command: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(part) for part in command], cwd=ROOT, **kwargs)


def commit() -> str:
    return run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def ops_token() -> str:
    for line in SECRETS.read_text().splitlines() if SECRETS.exists() else []:
        if line.startswith("PRODLIKE_OPS_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


# ── k6 ─────────────────────────────────────────────────────────────────────


def build_k6() -> int:
    """k6 with the event-stream extension, built for this machine by the xk6 image. A few minutes, once."""
    if K6.exists():
        print(f"{shown(K6)} is there: " + k6_version())
        return 0
    K6.parent.mkdir(parents=True, exist_ok=True)
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system(), "linux")
    machine = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    as_me = ["-u", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
    print(f"building k6 {K6_VERSION} with {SSE_EXTENSION} for {system}/{machine} — a few minutes…", flush=True)
    built = run(
        ["docker", "run", "--rm", "-e", f"GOOS={system}", "-e", f"GOARCH={machine}", *as_me]
        + ["-v", f"{K6.parent}:/xk6", XK6_IMAGE, "build", K6_VERSION, "--with", SSE_EXTENSION]
        + ["--output", f"/xk6/{K6.name}"]
    ).returncode
    if built == 0:
        print(k6_version())
    return built


def k6_version() -> str:
    said = run([K6, "version"], capture_output=True, text=True).stdout.strip().splitlines()
    return " · ".join(line.strip() for line in said if line.strip() and line.strip() != "Extensions:")


# ── The stack ──────────────────────────────────────────────────────────────


def small_pooler() -> Path:
    """The stack's own pooler settings with one number changed: as few server connections as the baseline had."""
    settings = POOLER_SETTINGS.read_text()
    small, changed = re.subn(
        r"^default_pool_size = \d+$", f"default_pool_size = {BASELINE_POOLER_CONNECTIONS}", settings, flags=re.M
    )
    assert changed == 1, f"{POOLER_SETTINGS.name} does not say default_pool_size once"
    SMALL_POOLER.parent.mkdir(parents=True, exist_ok=True)
    SMALL_POOLER.write_text(small)
    SMALL_POOLER.chmod(0o644)  # read by the pooler's own user, which is not this one
    return SMALL_POOLER


def compose(*args: str, env: dict | None = None, small_pools: bool = False) -> int:
    command = ["docker", "compose"]
    for file in (*COMPOSE_FILES, *([SMALL_POOLS] if small_pools else [])):
        command += ["-f", file]
    # Only the stack's own two secrets. Not the repository's .env: this stack is never shown a real key.
    command += ["--env-file", SECRETS]
    return run([*command, *args], env={**os.environ, **(env or {})}).returncode


def stand_ins_are_in() -> bool:
    log = run(["docker", "logs", BACKEND], capture_output=True, text=True)
    return STAND_INS_SAY in log.stdout + log.stderr


def up(small_pools: bool = False) -> int:
    prodlike = _script("prodlike")
    prodlike.make_secrets()
    if small_pools:
        small_pooler()
    for dockerfile, image in (
        ("src/backend/Dockerfile", DEPLOYED_IMAGE),
        ("docker/loadtest/Dockerfile", LOADTEST_IMAGE),
    ):
        print(f"building {image} from {dockerfile}…", flush=True)
        if run(["docker", "build", "-q", "-f", dockerfile, "-t", image, "."]).returncode != 0:
            return 1
    told = {
        "PRODLIKE_OTLP_ENDPOINT": os.environ.get("PRODLIKE_OTLP_ENDPOINT", "http://host.docker.internal:4318"),
        "PRODLIKE_COMMIT": os.environ.get("PRODLIKE_COMMIT", commit()),
    }
    if compose("up", "-d", "--build", env=told, small_pools=small_pools) != 0:
        print("docker compose could not start the stack — its own message is above.")
        return 1
    print("waiting for the backend to say it is ready…", flush=True)
    if not prodlike.wait_until_ready():
        print("the backend did not become ready. Its log:\n", flush=True)
        compose("logs", "--tail", "40", "backend")
        return 1
    if not stand_ins_are_in():
        print(f"the backend is up, but its log has no {STAND_INS_SAY} line: this is not the load test's image.")
        return 1
    _script("observability").hand_over_the_token()  # Prometheus reads this stack's metrics with its token
    pools = " The database pools are the baseline's: 4 at the pooler, 15 in the app." if small_pools else ""
    print(f"ready: {API} — the models and the providers are stand-ins.{pools}  Next: python scripts/loadtest.py smoke")
    return 0


# ── What the stack is doing ────────────────────────────────────────────────

PSQL = ["docker", "exec", "-e", "PGPASSWORD=standin_db_password", POSTGRES, "psql", "-X", "-A", "-F", "|"]
ASK_POOLER = [*PSQL, "host=pooler port=6432 dbname=pgbouncer user=tripplanner sslmode=require", "-c", "SHOW POOLS"]
ASK_POSTGRES = [
    *PSQL,
    "-t",
    "-U",
    "tripplanner",
    "-d",
    "tripplanner",
    "-c",
    "select coalesce(state, 'none'), count(*), coalesce(round(extract(epoch from max(now() - xact_start))::numeric, 1), 0) "
    "from pg_stat_activity where datname = 'tripplanner' and pid <> pg_backend_pid() group by 1",
]


def parse_pools(shown: str, database: str = "tripplanner") -> dict[str, int]:
    """PgBouncer's SHOW POOLS as psql prints it unaligned → the app database's row, by column name."""
    lines = [line for line in shown.strip().splitlines() if "|" in line]
    if len(lines) < 2:
        return {}
    columns = lines[0].split("|")
    for line in lines[1:]:
        row = dict(zip(columns, line.split("|")))
        if row.get("database") == database:
            return {name: int(value) for name, value in row.items() if re.fullmatch(r"-?\d+", value or "")}
    return {}


def parse_activity(shown: str) -> dict[str, dict[str, float]]:
    """pg_stat_activity grouped by state → {state: {"n": connections, "oldest_s": its oldest transaction}}."""
    states = {}
    for line in shown.strip().splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            states[parts[0]] = {"n": int(parts[1]), "oldest_s": float(parts[2])}
    return states


def parse_metrics(text: str) -> dict:
    """The few of the backend's own numbers a load test is read by, from what GET /metrics answers."""
    from prometheus_client.parser import text_string_to_metric_families

    seen: dict = {"lag_buckets": {}, "runs": {}, "requests": {}, "cache": {}}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            name, labels, value = sample.name, sample.labels, sample.value
            if name == "planning_runs_in_progress":
                seen["runs_in_progress"] = value
            elif name == "http_requests_inprogress":
                key = "streams_open" if labels.get("handler") == STREAM else "requests_in_progress"
                seen[key] = seen.get(key, 0) + value
            elif name in ("db_pool_connections", "redis_pool_connections"):
                seen[f"{name.split('_')[0]}_{labels.get('state')}"] = value
            elif name in ("db_pool_connections_limit", "redis_pool_connections_limit"):
                seen[f"{name.split('_')[0]}_limit"] = value
            elif name == "event_loop_lag_seconds_bucket":
                seen["lag_buckets"][labels["le"]] = value
            elif name == "process_cpu_seconds_total":
                seen["cpu_seconds"] = value
            elif name == "process_resident_memory_bytes":
                seen["memory_mb"] = round(value / 1e6, 1)
            elif name == "planning_run_duration_seconds_count":
                seen["runs"][labels.get("outcome")] = seen["runs"].get(labels.get("outcome"), 0) + value
            elif name == "http_requests_total":
                seen["requests"][labels.get("status")] = seen["requests"].get(labels.get("status"), 0) + value
            elif name == "mcp_cache_lookups_total":
                seen["cache"][labels.get("result")] = seen["cache"].get(labels.get("result"), 0) + value
    return seen


def parse_docker_stats(shown: str) -> dict[str, dict[str, float]]:
    """`docker stats --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}'` → {container: {"cpu": %, "mb": memory}}."""
    units = {"B": 1e-6, "kB": 1e-3, "KiB": 1.024e-3, "MB": 1, "MiB": 1.048576, "GB": 1e3, "GiB": 1073.741824}
    containers = {}
    for line in shown.strip().splitlines():
        parts = line.split("|")
        used = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", parts[2]) if len(parts) == 3 else None
        if used is None:
            continue
        short = re.sub(r"^tripplanner[-_](prodlike-|observability-)?|-1$", "", parts[0])
        containers[short] = {
            "cpu": float(parts[1].rstrip("%") or 0),
            "mb": round(float(used.group(1)) * units.get(used.group(2), 1), 1),
        }
    return containers


def quantile_from_buckets(before: dict[str, float], after: dict[str, float], q: float) -> float | None:
    """A quantile of what a Prometheus histogram observed between two readings: the upper edge of its bucket.

    Infinity when it fell past the last edge: the histogram only knows it was more than that.
    """
    grown = sorted((float(edge), after[edge] - before.get(edge, 0.0)) for edge in after)
    total = grown[-1][1] if grown else 0
    if total <= 0:
        return None
    return next(edge for edge, count in grown if count >= q * total)


def lateness(before: dict[str, float], after: dict[str, float]) -> dict[str, float | str | None]:
    """How late the event loop ran between two readings, in seconds: p50, p99 and the worst.

    A figure past the histogram's last edge is written as what is known of it: ">2.5".
    """
    edges = sorted(float(edge) for edge in after if float(edge) != float("inf"))
    told: dict[str, float | str | None] = {}
    for name, q in (("p50", 0.5), ("p99", 0.99), ("max", 1.0)):
        found = quantile_from_buckets(before, after, q)
        told[name] = f">{edges[-1]:g}" if found == float("inf") and edges else found
    return told


class Watch(threading.Thread):
    """Looks at the stack every two seconds for as long as it runs. `samples`: what it saw, and when."""

    def __init__(self, api: str, token: str) -> None:
        super().__init__(daemon=True)
        self.api, self.token = api, token
        self.samples: list[dict] = []
        self.containers: list[dict] = []
        self._halt = threading.Event()
        self._began = time.monotonic()

    def look(self) -> dict:
        at = round(time.monotonic() - self._began, 1)
        pooler = parse_pools(run(ASK_POOLER, capture_output=True, text=True).stdout)
        postgres = parse_activity(run(ASK_POSTGRES, capture_output=True, text=True).stdout)
        sample: dict = {"t": at, "pooler": pooler, "postgres": postgres}
        asked = time.perf_counter()
        try:
            answer = httpx.get(f"{self.api}/metrics", headers={"Authorization": f"Bearer {self.token}"}, timeout=8)
            sample["app"] = parse_metrics(answer.text) if answer.status_code == 200 else {}
        except Exception:
            sample["app"] = {}  # under enough load the backend does not answer this either — which is a finding
        sample["metrics_took_ms"] = round((time.perf_counter() - asked) * 1000)
        return sample

    def _docker(self) -> None:
        while not self._halt.is_set():
            shown = run(
                ["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"],
                capture_output=True,
                text=True,
            ).stdout
            self.containers.append({"t": round(time.monotonic() - self._began, 1), **parse_docker_stats(shown)})
            self._halt.wait(8)

    def run(self) -> None:
        threading.Thread(target=self._docker, daemon=True).start()
        while not self._halt.is_set():
            began = time.monotonic()
            try:
                self.samples.append(self.look())
            except Exception as exc:  # a look that fails is not a reason to stop looking
                self.samples.append({"t": round(time.monotonic() - self._began, 1), "error": repr(exc)[:200]})
            self._halt.wait(max(0.2, SAMPLE_EVERY_S - (time.monotonic() - began)))

    def stop(self) -> None:
        self._halt.set()
        self.join(15)

    def quiet(self) -> bool:
        """Nothing is being planned and no stream is open: the last run's load is gone."""
        last = next((s for s in reversed(self.samples) if s.get("app")), None)
        return bool(last) and not last["app"].get("runs_in_progress") and not last["app"].get("streams_open")


def _peak(values: list) -> float | None:
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _mean(values: list) -> float | None:
    values = [value for value in values if value is not None]
    return round(statistics.fmean(values), 2) if values else None


def summarise(samples: list[dict], containers: list[dict], until: float) -> dict:
    """What the stack did while the load was on (the first `until` seconds of the samples)."""
    during = [s for s in samples if s.get("t", 0) <= until and "error" not in s] or samples
    apps = [s["app"] for s in during if s.get("app")]

    def app(name: str) -> list:
        return [a.get(name) for a in apps]

    def pooler(name: str) -> list:
        return [s.get("pooler", {}).get(name) for s in during]

    in_transaction = [s.get("postgres", {}).get("idle in transaction", {}) for s in during]
    told = {
        "samples": len(during),
        "metrics_unanswered": sum(1 for s in during if not s.get("app")),
        "metrics_took_ms_max": _peak([s.get("metrics_took_ms") for s in during]),
        "plans_in_progress": {"max": _peak(app("runs_in_progress")), "mean": _mean(app("runs_in_progress"))},
        "streams_open": {"max": _peak(app("streams_open")), "mean": _mean(app("streams_open"))},
        "db_pool": {
            "in_use_max": _peak(app("db_in_use")),
            "in_use_mean": _mean(app("db_in_use")),
            "limit": _peak(app("db_limit")),
        },
        "redis_pool": {"in_use_max": _peak(app("redis_in_use")), "limit": _peak(app("redis_limit"))},
        "pooler": {
            "clients_served_max": _peak(pooler("cl_active")),
            "clients_waiting_max": _peak(pooler("cl_waiting")),
            "clients_waiting_mean": _mean(pooler("cl_waiting")),
            "server_connections_in_use_max": _peak(pooler("sv_active")),
            "longest_wait_s": _peak(pooler("maxwait")),
        },
        "postgres": {
            "idle_in_transaction_max": _peak([state.get("n", 0) for state in in_transaction]),
            "idle_in_transaction_mean": _mean([state.get("n", 0) for state in in_transaction]),
            "oldest_open_transaction_s": _peak([state.get("oldest_s", 0) for state in in_transaction]),
        },
    }
    if len(apps) >= 2:
        first, last = apps[0], apps[-1]
        seconds = max(1e-9, during[-1]["t"] - during[0]["t"])
        if "cpu_seconds" in first and "cpu_seconds" in last:
            told["backend_cpu_cores"] = round((last["cpu_seconds"] - first["cpu_seconds"]) / seconds, 2)
        told["backend_memory_mb_max"] = _peak(app("memory_mb"))
        told["event_loop_lag_s"] = lateness(first.get("lag_buckets", {}), last.get("lag_buckets", {}))
        for name in ("runs", "requests", "cache"):
            told[name] = {
                key: last.get(name, {}).get(key, 0) - first.get(name, {}).get(key, 0) for key in last.get(name, {})
            }
    names = sorted({name for seen in containers for name in seen if name != "t"})
    told["containers"] = {
        name: {
            "cpu_max": _peak([seen.get(name, {}).get("cpu") for seen in containers]),
            "mb_max": _peak([seen.get(name, {}).get("mb") for seen in containers]),
        }
        for name in names
    }
    told["docker_memory_mb_max"] = _peak(
        [round(sum(c["mb"] for name, c in seen.items() if name != "t"), 1) for seen in containers]
    )
    return told


def compact(samples: list[dict]) -> list[dict]:
    """The samples as a time series small enough to keep: one line a look."""
    series = []
    for s in samples:
        app, pooler, postgres = s.get("app") or {}, s.get("pooler") or {}, s.get("postgres") or {}
        series.append(
            {
                "t": s.get("t"),
                "plans": app.get("runs_in_progress"),
                "streams": app.get("streams_open"),
                "db_in_use": app.get("db_in_use"),
                "redis_in_use": app.get("redis_in_use"),
                "pooler_serving": pooler.get("cl_active"),
                "pooler_waiting": pooler.get("cl_waiting"),
                "pooler_longest_wait_s": pooler.get("maxwait"),
                "idle_in_transaction": postgres.get("idle in transaction", {}).get("n", 0),
                "metrics_took_ms": s.get("metrics_took_ms"),
            }
        )
    return series


# ── A run ──────────────────────────────────────────────────────────────────


def figures(summary: dict) -> dict:
    """k6's summary → the figures a run is read by. Durations in milliseconds."""
    metrics, hold = summary["metrics"], summary["hold"]

    def of(name: str, value: str, default=None):
        return metrics.get(name, {}).get(value, default)

    def spread(name: str) -> dict:
        return {
            short: (round(of(name, long)) if of(name, long) is not None else None)
            for short, long in (("p50", "med"), ("p95", "p(95)"), ("p99", "p(99)"), ("max", "max"))
        } | {"count": of(name, "count", 0)}

    # a visit that could not start — every virtual user still busy with an earlier one — was offered all the same
    offered = of("visits_started", "count", 0) + of("dropped_iterations", "count", 0)
    completed, asked = of("plans_completed", "count", 0), of("plans_asked", "count", 0)
    endpoints = {}
    for name in metrics:
        found = re.fullmatch(r"http_req_duration\{name:(.+)\}", name)
        if found:
            endpoints[found.group(1)] = spread(name) | {
                "failed": round(of(f"http_req_failed{{name:{found.group(1)}}}", "rate", 0.0), 4)
            }
    return {
        "visits_offered": offered,
        "visits_dropped": of("dropped_iterations", "count", 0),
        "plans_asked": asked,
        "plans_completed": completed,
        "plans_failed": {
            why: of(f"plans_failed{{why:{why}}}", "count", 0) for why in ("refused", "failed", "gave_up", "no_stream")
        },
        "plans_arrived_of_offered": round(completed / offered, 4) if offered else None,
        "plans_per_second": round(completed / hold, 3),
        "plan_ms": spread("plan_duration"),
        "plan_first_piece_ms": spread("plan_first_piece"),
        "stream_opened_ms": spread("stream_opened"),
        "api": spread("http_req_duration{kind:api}")
        | {
            "requests": of("http_reqs{kind:api}", "count", 0),
            "per_second": round(of("http_reqs{kind:api}", "count", 0) / hold, 2),
            "failed": round(of("http_req_failed{kind:api}", "rate", 0.0), 4),
        },
        "endpoints": endpoints,
        "vus_max": of("vus_max", "max") or of("vus_max", "value"),
    }


def judge(k6: dict) -> list[str]:
    """Why a rate did not hold; nothing when it did."""
    why = []
    if (k6["plans_arrived_of_offered"] or 0) < PLANS_MUST_ARRIVE:
        why.append(f"{k6['plans_completed']} of {k6['visits_offered']} visits got a plan")
    if k6["visits_dropped"]:
        why.append(f"{k6['visits_dropped']} visits could not even start")
    if (k6["plan_ms"]["p95"] or 0) > PLAN_P95_S * 1000:
        why.append(f"a plan took {k6['plan_ms']['p95'] / 1000:.0f} s at p95")
    if (k6["api"]["p99"] or 0) > API_P99_MS:
        why.append(f"requests took {k6['api']['p99']} ms at p99")
    if k6["api"]["failed"] > API_ERRORS:
        why.append(f"{k6['api']['failed']:.1%} of requests failed")
    return why


def make_accounts(how_many: int) -> Path:
    """Accounts for the travellers, made before the load starts: a file of [{email, password, token}].

    Here and not in the k6 script: making an account hashes a password, and for that fifth of a
    second the server does nothing else. Enough of them that each is used a few times, whatever
    the rate — so a traveller's list of trips is as long at one rate as at another.
    """
    made, tag = [], secrets.token_hex(4)
    with httpx.Client(base_url=API, timeout=60) as http:
        for n in range(how_many):
            account = {"email": f"loadtest-{tag}-{n}@example.com", "password": secrets.token_urlsafe(16)}
            http.post("/auth/register", json=account).raise_for_status()
            login = http.post("/auth/login", data={"username": account["email"], "password": account["password"]})
            made.append({**account, "token": login.raise_for_status().json()["access_token"]})
    accounts = WORK / "accounts.json"  # git-ignored: passwords and tokens of this machine's stand-in stack
    accounts.parent.mkdir(parents=True, exist_ok=True)
    accounts.write_text(json.dumps(made))
    return accounts


def accounts_for(rate: float, hold: int) -> int:
    """One account for every three or four visits: a traveller who comes back, not one who never leaves."""
    return max(5, round(rate * hold / 3.5))


def run_k6(
    rate: float, hold: int, summary: Path, accounts: Path, smoke: bool = False, options: dict | None = None
) -> int:
    if not K6.exists():
        print("no k6 with the event-stream extension yet — python scripts/loadtest.py k6")
        return 2
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.unlink(missing_ok=True)
    told = {"BASE_URL": API, "RATE": rate, "HOLD": hold, "SUMMARY": summary, "ACCOUNTS": accounts, **(options or {})}
    if smoke:
        told["SMOKE"] = 1
    command = [K6, "run", "--no-usage-report", "--quiet"]
    for name, value in told.items():
        command += ["-e", f"{name}={value}"]
    log = WORK / "logs" / f"{summary.stem}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as out:
        code = run([*command, SCRIPT], stdout=out, stderr=subprocess.STDOUT).returncode
    said = log.read_text()
    print("\n".join(line for line in said.splitlines() if line.startswith("  ")), flush=True)
    problems = [line for line in said.splitlines() if "level=error" in line or "level=warning" in line]
    if problems:
        print(f"  k6 said {len(problems)} warning(s) or error(s) — {shown(log)}; the first: {problems[0][:160]}")
    return code


def a_run(name: str, rate: float, hold: int, options: dict | None = None, settle: float = 240.0) -> dict | None:
    """Visits started at `rate` a second for `hold` seconds, watched; then the stack is left to finish what it has."""
    if not stand_ins_are_in():
        print("the load test's stack is not up (python scripts/loadtest.py up) — the real providers cannot take this.")
        return None
    out = RESULTS / name / f"rate-{rate:g}.json"
    summary = WORK / "summaries" / f"{name}-rate-{rate:g}.json"
    print(f"\n── {name}: {rate:g} visits a second for {hold} s ──", flush=True)
    staying_up = kept_awake(hold + GIVE_UP_AFTER_S + settle + 120)
    awake = Awake()
    accounts = make_accounts(accounts_for(rate, hold))  # before anyone is watching: it is not part of the load
    if awake.slept():
        print(
            f"  this machine fell asleep for {awake.asleep_for():.0f} s while the accounts were being made — "
            "is its lid closed, on battery? Nothing measured on a machine that sleeps means anything: not started."
        )
        return None
    watch = Watch(API, ops_token())
    began = datetime.now(timezone.utc)
    watch.start()
    code = run_k6(rate, hold, summary, accounts, options=options)
    loaded_for = time.monotonic() - watch._began
    waited = time.monotonic()
    while not watch.quiet() and time.monotonic() - waited < settle:
        time.sleep(SAMPLE_EVERY_S)
    left = None if watch.quiet() else (watch.samples[-1].get("app") or {})
    watch.stop()
    if staying_up is not None:
        staying_up.terminate()
    if awake.slept():
        print(
            f"  this machine was asleep for {awake.asleep_for():.0f} s of the run: its figures are wrong, and are not kept. "
            "Keep it awake — lid open, plugged in — and run it again."
        )
        return None
    if code != 0 or not summary.exists():
        print(f"  k6 ended with {code} and left no summary — see .loadtest/logs/")
        return None

    k6 = figures(json.loads(summary.read_text()))
    why_not = judge(k6)
    result = {
        "name": name,
        "rate": rate,
        "hold_s": hold,
        "began": began.isoformat(timespec="seconds"),
        "commit": commit(),
        "options": {key: str(value) for key, value in (options or {}).items()},
        "holds": not why_not,
        "why_not": why_not,
        "k6": k6,
        "stack": summarise(watch.samples, watch.containers, loaded_for),
        "left_unfinished": {"plans": left.get("runs_in_progress"), "streams": left.get("streams_open")}
        if left
        else None,
        "series": compact(watch.samples),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n")
    stack = result["stack"]
    print(
        f"  the stack: plans in progress up to {stack['plans_in_progress']['max']}, streams {stack['streams_open']['max']}, "
        f"database pool {stack['db_pool']['in_use_max']}/{stack['db_pool']['limit']}, "
        f"pooler serving {stack['pooler']['clients_served_max']} with {stack['pooler']['clients_waiting_max']} waiting "
        f"(longest {stack['pooler']['longest_wait_s']} s), backend CPU {stack.get('backend_cpu_cores')} cores, "
        f"event loop late by {late((stack.get('event_loop_lag_s') or {}).get('p99'))} at p99"
    )
    print(
        f"  {'HOLDS' if not why_not else 'DOES NOT HOLD: ' + '; '.join(why_not)}   → {shown(out)}",
        flush=True,
    )
    return result


def ladder(name: str, rates: list[float], hold: int, keep_going: bool, options: dict | None = None) -> int:
    for rate in rates:
        result = a_run(name, rate, hold, options)
        if result is None:
            return 1
        fell_over = (result["k6"]["plans_arrived_of_offered"] or 0) < 0.5
        if fell_over and not keep_going:
            print(
                f"\nat {rate:g} a second fewer than half the visits got a plan: higher rates are not tried (--keep-going does)."
            )
            break
    print()
    table([name])
    return 0


# ── Reading the runs back ──────────────────────────────────────────────────


def runs_of(name: str) -> list[dict]:
    found = [json.loads(path.read_text()) for path in (RESULTS / name).glob("rate-*.json")]
    return sorted(found, key=lambda result: result["rate"])


def _s(milliseconds) -> str:
    return "—" if milliseconds is None else f"{milliseconds / 1000:.1f} s"


def late(seconds) -> str:
    """A lateness as the table says it: "25 ms", "over 2500 ms", "—"."""
    if seconds is None:
        return "—"
    if isinstance(seconds, str):
        return f"over {float(seconds.lstrip('>')) * 1000:g} ms"
    return f"{seconds * 1000:g} ms"


def _ms(milliseconds) -> str:
    return "—" if milliseconds is None else f"{milliseconds:,.0f} ms"


def table(names: list[str]) -> None:
    """The runs kept under these names, as Markdown — what docs/load-test-report.md is written from."""
    for name in names:
        results = runs_of(name)
        if not results:
            print(f"no runs kept as {name!r} in {shown(RESULTS)}/")
            continue
        print(f"### {name}\n")
        print(
            "| Visits a second | Plans made | Plans a second | A plan takes, p50 / p95 | Requests a second | Request p50 / p95 / p99 | Failed | Holds |"
        )
        print("|---|---|---|---|---|---|---|---|")
        for r in results:
            k6 = r["k6"]
            print(
                f"| {r['rate']:g} | {k6['plans_completed']} of {k6['visits_offered']} | {k6['plans_per_second']:.2f} "
                f"| {_s(k6['plan_ms']['p50'])} / {_s(k6['plan_ms']['p95'])} | {k6['api']['per_second']:.1f} "
                f"| {_ms(k6['api']['p50'])} / {_ms(k6['api']['p95'])} / {_ms(k6['api']['p99'])} | {k6['api']['failed']:.1%} "
                f"| {'yes' if r['holds'] else 'no'} |"
            )
        print()
        print(
            "| Visits a second | Plans at once, max | Streams open, max | Database pool in use, max | Pooler: served / waiting, max | Longest wait at the pooler | Idle in transaction, max | Event loop late, p99 | Backend CPU |"
        )
        print("|---|---|---|---|---|---|---|---|---|")
        for r in results:
            s = r["stack"]
            lag = (s.get("event_loop_lag_s") or {}).get("p99")
            print(
                f"| {r['rate']:g} | {s['plans_in_progress']['max']:g} | {s['streams_open']['max']:g} "
                f"| {s['db_pool']['in_use_max']:g} of {s['db_pool']['limit']:g} "
                f"| {s['pooler']['clients_served_max']} / {s['pooler']['clients_waiting_max']} | {s['pooler']['longest_wait_s']} s "
                f"| {s['postgres']['idle_in_transaction_max']} | {late(lag)} "
                f"| {s.get('backend_cpu_cores', '—')} cores |"
            )
        print()


# ── One plan, step by step ─────────────────────────────────────────────────


def one_plan(api: str, destination: str, out: Path | None) -> int:
    """One plan, made as the trip page makes it, with every event's arrival and what Postgres saw meanwhile.

    On the load test's stack it shows what the stand-ins take; with scripts/prodlike.py's stack up
    instead, what the real models and providers take — the two are meant to look alike.
    """
    account = {"email": f"one-plan-{secrets.token_hex(5)}@example.com", "password": secrets.token_urlsafe(16)}
    seen: list[dict] = []
    stop, connected, finished, sent = threading.Event(), threading.Event(), threading.Event(), {}

    def postgres() -> None:
        while not stop.is_set():
            states = parse_activity(run(ASK_POSTGRES, capture_output=True, text=True).stdout)
            seen.append({"t": round(time.monotonic() - sent.get("at", time.monotonic()), 2), **states})
            stop.wait(0.5)

    with httpx.Client(base_url=api, timeout=60) as http:
        http.post("/auth/register", json=account).raise_for_status()
        login = http.post("/auth/login", data={"username": account["email"], "password": account["password"]})
        token = login.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        start = date.today() + timedelta(days=45)
        trip = http.post(
            "/trips",
            headers=auth,
            json={
                "destination": destination,
                "start_date": str(start),
                "end_date": str(start + timedelta(days=3)),
                "budget": 70_000,
                "group_size": 2,
                "interests": ["history", "food"],
            },
        )
        trip.raise_for_status()
        trip_id = trip.json()["id"]
        events: list[dict] = []

        def listen() -> None:
            timeout = httpx.Timeout(30, read=240)
            with httpx.stream(
                "GET", f"{api}/trips/{trip_id}/stream", params={"token": token}, timeout=timeout
            ) as answer:
                kind = None
                for line in answer.iter_lines():
                    if line.startswith("event:"):
                        kind = line[6:].strip()
                    elif line.startswith("data:") and kind == "connected":
                        connected.set()
                    elif line.startswith("data:"):
                        said = json.loads(line[5:].strip())
                        what = said.get("event") or f"{said.get('agent')}: {said.get('status')}"
                        events.append(
                            {"t": round(time.monotonic() - sent.get("at", time.monotonic()), 3), "event": what}
                        )
                        if said.get("event") in ("planning_complete", "planning_failed"):
                            finished.set()
                            return

        threading.Thread(target=listen, daemon=True).start()
        connected.wait(30)
        print(
            "the stream is open; 8 s for the traveller to write (the trip's searches are warmed meanwhile)…", flush=True
        )
        time.sleep(8)
        threading.Thread(target=postgres, daemon=True).start()
        sent["at"] = time.monotonic()
        asked = http.post(
            f"/trips/{trip_id}/plan", headers=auth, json={"raw_input": "A relaxed long weekend of forts and good food"}
        )
        finished.wait(240)
        took = round(time.monotonic() - sent["at"], 2)
        time.sleep(5)  # the preferences and the embeddings come after planning_complete
        stop.set()
        runs = http.get(f"/trips/{trip_id}/runs", headers=auth).json()

    pieces = [event for event in events if event["event"] == "builder_token"]
    others = [event for event in events if event["event"] != "builder_token"]
    held = [look["t"] for look in seen if look.get("idle in transaction", {}).get("n")]
    result = {
        "api": api,
        "destination": destination,
        "trip_id": trip_id,
        "asked": asked.status_code,
        "plan_seconds": took,
        "events": others,
        "pieces": {
            "count": len(pieces),
            "from_s": pieces[0]["t"] if pieces else None,
            "to_s": pieces[-1]["t"] if pieces else None,
        },
        "steps": [
            {"step": r["agent_name"], "status": r["status"], "ms": r["duration_ms"], "tokens": r["token_count"]}
            for r in runs
        ],
        "a_connection_idle_in_transaction": {"from_s": min(held), "to_s": max(held)} if held else None,
        "postgres": seen,
    }
    print(f"\nthe plan took {took} s: {len(events)} events, {len(pieces)} of them pieces of the itinerary")
    for event in others:
        print(f"  {event['t']:>7.2f} s  {event['event']}")
    for step in result["steps"]:
        print(f"  {step['step']:<26} {step['status']:<10} {step['ms']:>6} ms  {step['tokens'] or ''}")
    if held:
        print(f"Postgres: a connection idle inside a transaction from {min(held)} s to {max(held)} s after POST /plan")
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=1) + "\n")
        print(f"→ {out}")
    print(f"trip {trip_id}")
    return 0 if events and events[-1]["event"] == "planning_complete" else 1


def smoke() -> int:
    """One traveller, three visits. Every part has to work: accounts, the stream, three plans, every request."""
    if not stand_ins_are_in():
        print("the load test's stack is not up — python scripts/loadtest.py up")
        return 1
    summary = WORK / "summaries" / "smoke.json"
    if run_k6(0, 0, summary, make_accounts(1), smoke=True) != 0 or not summary.exists():
        return 1
    metrics = json.loads(summary.read_text())["metrics"]
    completed = metrics.get("plans_completed", {}).get("count", 0)
    failed = metrics.get("http_req_failed{kind:api}", {}).get("rate", 1.0)
    for name in sorted(metrics):
        found = re.fullmatch(r"http_req_duration\{name:(.+)\}", name)
        if found:
            print(
                f"    {found.group(1):<28} {metrics[name].get('count', 0):>3} × {_ms(metrics[name].get('med')):>8} median"
            )
    ok = completed == 3 and failed == 0
    print(
        f"  {'✓' if ok else '✗'} three visits, three plans, no failed request"
        + ("" if ok else f" — {completed} plans, {failed:.1%} failed")
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="The load test: its stack, the load, and what the stack did under it.")
    commands = parser.add_subparsers(dest="what", required=True)
    commands.add_parser("k6", help="build the k6 that can read an event stream")
    started = commands.add_parser("up", help="start the production-like stack with the stand-ins in")
    started.add_argument("--small-pools", action="store_true", help="with the baseline's database connections: 4 + 15")
    commands.add_parser("down", help="stop it and delete what it made")
    commands.add_parser("smoke", help="one traveller, three visits")
    plan = commands.add_parser("one-plan", help="one plan, timed step by step")
    plan.add_argument("--api", default=API)
    plan.add_argument("--destination", default="Jaipur")
    plan.add_argument("--out", type=Path)
    for name, about in (("run", "one rate"), ("ladder", "one rate after another")):
        loaded = commands.add_parser(name, help=about)
        loaded.add_argument("--name", default="run", help="what the run is kept as, under docs/loadtest/")
        loaded.add_argument("--hold", type=int, default=150, help="seconds each rate is kept up")
        loaded.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="passed to the k6 script")
        if name == "run":
            loaded.add_argument("--rate", type=float, required=True, help="visits started a second")
        else:
            loaded.add_argument(
                "--rates", default="0.1,0.25,0.5,1,2", help="visits started a second, one after another"
            )
            loaded.add_argument(
                "--keep-going", action="store_true", help="try the higher rates even after one falls over"
            )
    shown = commands.add_parser("table", help="the runs kept, as Markdown")
    shown.add_argument("names", nargs="+")
    args = parser.parse_args()

    if args.what == "k6":
        return build_k6()
    if args.what == "up":
        return up(args.small_pools)
    if args.what == "down":
        return compose("down", "-v", "--remove-orphans")
    if args.what == "smoke":
        return smoke()
    if args.what == "one-plan":
        return one_plan(args.api.rstrip("/"), args.destination, args.out)
    if args.what == "table":
        table(args.names)
        return 0
    options = dict(pair.split("=", 1) for pair in args.set)
    if args.what == "run":
        return 0 if a_run(args.name, args.rate, args.hold, options) else 1
    return ladder(args.name, [float(rate) for rate in args.rates.split(",")], args.hold, args.keep_going, options)


if __name__ == "__main__":
    sys.exit(main())

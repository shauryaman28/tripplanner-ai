"""What this app counts about itself, for Prometheus to read at GET /metrics (Phase 29).

Two kinds of metric come out of that address.

The requests — how many, how many failed, how long (rate, errors, duration: "RED") — are counted by
`prometheus-fastapi-instrumentator`, per endpoint as the router names it (`/trips/{trip_id}/plan`,
never an address with an id in it):

    http_requests_total{handler, method, status}        status is 2xx / 4xx / 5xx
    http_request_duration_seconds{handler, method}      a histogram: p50 / p95 / p99 come from it
    http_requests_inprogress{handler, method}           an open event stream is a request in progress

The rest are the app's own, counted where the thing happens:

    llm_calls_total{agent, model, status}               every call to a model: ok, or error
    llm_tokens_total{agent, model, direction}           what was sent (input) and what came back (output)
    llm_cost_usd_total{agent, model}                    the same tokens at list price (src/ai/usage.py)
    mcp_tool_calls_total{tool, status}                  every call to the tool server
    mcp_tool_call_duration_seconds{tool}
    mcp_cache_lookups_total{cache, result}              hit, miss, or error (Redis could not be asked)
    mcp_cache_hit_ratio                                 hits ÷ lookups over the last five minutes
    itinerary_builds_total{status}                      success, failure, or retry (sent back by the evaluator)
    planning_run_duration_seconds{kind, outcome}        a whole run: the request that starts one is
    planning_runs_in_progress                           answered at once, so its own duration says nothing
    tripplanner_build_info{version, commit}             always 1: which build these numbers are about

And three that say how near this process is to running out of something (Phase 30) — what a load
test watches, and what "it got slow" is usually made of:

    db_pool_connections{state}                          the database pool's connections: in_use, idle
    db_pool_connections_limit                           the most it will open: its size plus its overflow
    redis_pool_connections{state}                       the same for Redis — an open event stream holds one
    redis_pool_connections_limit
    event_loop_lag_seconds                              how late the event loop ran what was due: every
                                                        request of this process waits behind whatever holds it

A request's duration is the time to its first byte, not its last: an event stream is open for
minutes by design, and those minutes are not latency.

`mcp_cache_hit_ratio` is the roadmap's gauge, and it is a ratio over a window because a ratio since
the process started stops moving: after a good week, an hour in which every lookup missed would
barely show. With no lookup in the window it is NaN — "nobody asked" is not "nothing was found",
and an alert on a low ratio must not fire at night. The counter beside it is what a dashboard
should prefer: `rate()` of it is right over any window, and across several instances.

A counter exists from its first increment, and Prometheus cannot see an increase from "not there" to
1: `rate()` and `increase()` start counting at the second. On a busy system that is one event lost;
on a quiet one it is the first plan after every restart missing from its chart, and the first error
missing from its alert. So every series whose labels can be listed is made at zero before anything
happens — the run kinds and outcomes and the build statuses here, the tools and caches by the tool
client (`expect_tools`), the agents and their models by src/ai/llm.py (`expect_llm_calls`). What
cannot be listed — an endpoint's requests — is the library's, and loses its first.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import prometheus_client
from prometheus_client import Counter, Gauge, Histogram

# Every counter would otherwise come with a second series saying when it was made: twice the series, read by nobody.
prometheus_client.disable_created_metrics()

REQUEST_BUCKETS = (0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
TOOL_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40)
RUN_BUCKETS = (2, 4, 6, 8, 10, 12.5, 15, 17.5, 20, 25, 30, 45, 60, 90, 120, 180, 300)  # a plan is 8 to 25 s: fine there
LAG_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5)
CACHE_WINDOW_S = 300.0
LAG_EVERY_S = 0.1  # how often the event loop is asked to wake something, to see how late it does
POOL_STATES = ("in_use", "idle")

BUILD_STATUSES = ("success", "failure", "retry")
CACHE_RESULTS = ("hit", "miss", "error")
RUN_KINDS = ("plan", "replan", "refine", "retry")
RUN_OUTCOMES = ("completed", "no_plan", "crashed", "interrupted")
CALL_STATUSES = ("ok", "error")

LLM_CALLS = Counter(
    "llm_calls_total", "Calls to a language model, by the agent that asked.", ["agent", "model", "status"]
)
LLM_TOKENS = Counter(
    "llm_tokens_total", "Tokens sent to (input) and received from (output) a model.", ["agent", "model", "direction"]
)
LLM_COST = Counter("llm_cost_usd_total", "What those tokens cost at list price, in US dollars.", ["agent", "model"])
TOOL_CALLS = Counter("mcp_tool_calls_total", "Calls to the tool server.", ["tool", "status"])
TOOL_SECONDS = Histogram(
    "mcp_tool_call_duration_seconds", "How long a call to the tool server took.", ["tool"], buckets=TOOL_BUCKETS
)
CACHE_LOOKUPS = Counter(
    "mcp_cache_lookups_total", "Lookups in the tool server's cache: hit, miss, or error.", ["cache", "result"]
)
CACHE_HIT_RATIO = Gauge(
    "mcp_cache_hit_ratio", "Cache hits divided by lookups over the last five minutes; NaN when there were none."
)
BUILDS = Counter(
    "itinerary_builds_total", "Itineraries built: saved, given up, or sent back to be built again.", ["status"]
)
RUN_SECONDS = Histogram(
    "planning_run_duration_seconds",
    "How long a planning run took, start to end.",
    ["kind", "outcome"],
    buckets=RUN_BUCKETS,
)
RUNS_IN_PROGRESS = Gauge("planning_runs_in_progress", "Planning runs this process is making right now.")
BUILD_INFO = Gauge(
    "tripplanner_build_info", "Always 1: the version and the commit that are running.", ["version", "commit"]
)
DB_POOL = Gauge(
    "db_pool_connections",
    "Connections of this process's database pool: lent out (in_use), or waiting (idle).",
    ["state"],
)
DB_POOL_LIMIT = Gauge("db_pool_connections_limit", "The most connections the database pool will open.")
REDIS_POOL = Gauge(
    "redis_pool_connections",
    "Connections of this process's Redis pool: in use — one per open stream — or idle.",
    ["state"],
)
REDIS_POOL_LIMIT = Gauge("redis_pool_connections_limit", "The most connections the Redis pool will open.")
LOOP_LAG = Histogram("event_loop_lag_seconds", "How late the event loop ran a task that was due.", buckets=LAG_BUCKETS)

# A build that has never failed is 0 failures, not no data: these exist from the start.
for _status in BUILD_STATUSES:
    BUILDS.labels(_status)
for _kind in RUN_KINDS:
    for _outcome in RUN_OUTCOMES:
        RUN_SECONDS.labels(_kind, _outcome)


def expect_tools(tools: Iterable[str], caches: Iterable[str]) -> None:
    """These tools will be called and these caches looked up: their counters exist, at zero, from now."""
    for tool in tools:
        TOOL_SECONDS.labels(tool)
        for status in CALL_STATUSES:
            TOOL_CALLS.labels(tool, status)
    for cache in caches:
        for result in CACHE_RESULTS:
            CACHE_LOOKUPS.labels(cache, result)


def expect_llm_calls(asked_by: Mapping[str, Iterable[str]]) -> None:
    """{agent: the models it asks}: each one's counters exist, at zero, from now — calls (errors included),
    tokens and cost."""
    for agent, models in asked_by.items():
        for model in (name.removeprefix("models/") for name in models):
            for status in CALL_STATUSES:
                LLM_CALLS.labels(agent, model, status)
            for direction in ("input", "output"):
                LLM_TOKENS.labels(agent, model, direction)
            LLM_COST.labels(agent, model)


class Window:
    """What happened in the last so many seconds: how many of them were hits."""

    def __init__(self, seconds: float = CACHE_WINDOW_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._seconds, self._clock = seconds, clock
        self._seen: deque[tuple[float, int, int]] = deque()  # (when, hits, lookups)
        self._lock = threading.Lock()

    def add(self, hits: int, lookups: int) -> None:
        with self._lock:
            self._seen.append((self._clock(), hits, lookups))

    def ratio(self) -> float:
        with self._lock:
            oldest = self._clock() - self._seconds
            while self._seen and self._seen[0][0] < oldest:
                self._seen.popleft()
            lookups = sum(entry[2] for entry in self._seen)
            return sum(entry[1] for entry in self._seen) / lookups if lookups else math.nan


cache_window = Window()
CACHE_HIT_RATIO.set_function(cache_window.ratio)


def llm_call(
    agent: str | None,
    model: str | None,
    ok: bool,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float | None = None,
) -> None:
    """One call to a model, as it ended. `agent`: whose work it was part of; "unmetered" when nobody's."""
    agent, model = agent or "unmetered", model or "unknown"
    LLM_CALLS.labels(agent, model, "ok" if ok else "error").inc()
    if tokens_in:
        LLM_TOKENS.labels(agent, model, "input").inc(tokens_in)
    if tokens_out:
        LLM_TOKENS.labels(agent, model, "output").inc(tokens_out)
    if cost_usd:
        LLM_COST.labels(agent, model).inc(cost_usd)


def tool_call(tool: str, ok: bool, seconds: float) -> None:
    TOOL_CALLS.labels(tool, "ok" if ok else "error").inc()
    TOOL_SECONDS.labels(tool).observe(seconds)


def cache_lookups(cache: str, result: str, count: int = 1) -> None:
    """`count` lookups in one of the tool server's caches ("flights", "geocode", …) that ended the same way."""
    if count <= 0 or result not in CACHE_RESULTS:
        return
    CACHE_LOOKUPS.labels(cache, result).inc(count)
    cache_window.add(count if result == "hit" else 0, count)


def itinerary_build(status: str) -> None:
    """A build's fate: "success" (saved), "failure" (given up), "retry" (sent back, to be built again)."""
    BUILDS.labels(status).inc()


class Run:
    """A planning run being timed. `outcome` is what it will be counted as when the block ends."""

    def __init__(self) -> None:
        self.outcome = "completed"


@contextmanager
def planning_run(kind: str) -> Iterator[Run]:
    """Time a planning run. Leaving by an error is "crashed", by cancellation "interrupted", unless the block said."""
    run, started = Run(), time.monotonic()
    RUNS_IN_PROGRESS.inc()
    try:
        yield run
    except Exception:
        run.outcome = "crashed"
        raise
    except BaseException:  # the task was cancelled: the server is stopping
        run.outcome = "interrupted"
        raise
    finally:
        RUNS_IN_PROGRESS.dec()
        RUN_SECONDS.labels(kind, run.outcome).observe(time.monotonic() - started)


def build_info(version: str, commit: str | None) -> None:
    BUILD_INFO.labels(version, (commit or "")[:12] or "unknown").set(1)


# ── What runs out under load (Phase 30) ────────────────────────────────────


def _read(count: Callable[[], Any]) -> Callable[[], float]:
    """A gauge's reading that cannot fail a scrape: NaN when there is nothing to count, or it cannot be counted."""

    def reading() -> float:
        try:
            return float(count())
        except Exception:
            return math.nan

    return reading


def watch_db_pool(pool_of: Callable[[], Any]) -> None:
    """Report the database pool at every scrape. `pool_of()` returns the engine's pool as it is then.

    Read when Prometheus asks, not counted as connections come and go: the pool already knows.
    The limit is the pool's size plus its overflow — past it a request waits for a connection,
    and after the pool's timeout fails.
    """
    DB_POOL.labels("in_use").set_function(_read(lambda: pool_of().checkedout()))
    DB_POOL.labels("idle").set_function(_read(lambda: pool_of().checkedin()))
    DB_POOL_LIMIT.set_function(_read(lambda: pool_of().size() + max(0, pool_of()._max_overflow)))


def watch_redis_pool(pool_of: Callable[[], Any]) -> None:
    """Report the Redis client's pool at every scrape. `pool_of()` returns it, or None while there is no client.

    Every command borrows a connection for a moment; a subscription — an open event stream — keeps
    one. The limit is not a queue: a command over it fails at once ("Too many connections").
    """
    REDIS_POOL.labels("in_use").set_function(_read(lambda: len(pool_of()._in_use_connections)))
    REDIS_POOL.labels("idle").set_function(_read(lambda: len(pool_of()._available_connections)))
    REDIS_POOL_LIMIT.set_function(_read(lambda: pool_of().max_connections))


async def watch_event_loop(every: float = LAG_EVERY_S) -> None:
    """For as long as it runs: ask to be woken in `every` seconds, and note how much later than that it was.

    One event loop serves every request of a process, one piece of work at a time. Work that does
    not give it back — a password being hashed, a large answer being put together — makes
    everything else late by as long as it takes, and no request's own timing says why. This does.
    """
    loop = asyncio.get_running_loop()
    while True:
        due = loop.time() + every
        await asyncio.sleep(every)
        LOOP_LAG.observe(max(0.0, loop.time() - due))


_requests: Any = None  # what counts a finished request — made once for the process
_in_progress: Gauge | None = None


def instrument_app(app: Any, guard: Callable[..., Any]) -> None:
    """Count every request to a FastAPI app, and answer GET /metrics — to whoever `guard` lets in.

    `guard` is a FastAPI dependency: it raises for a caller who may not read the metrics.

    The library's one-liner is `Instrumentator().instrument(app).expose(app)`. Its middleware makes
    its metrics as it is built, and a metric can be registered once: a second app in the process —
    which is what the test suite has, the same module under two import names — failed on its first
    request. So the metrics are made here, once, and every app's middleware is handed them.
    """
    from fastapi import Depends
    from prometheus_fastapi_instrumentator import Instrumentator
    from prometheus_fastapi_instrumentator import metrics as request_metrics
    from prometheus_fastapi_instrumentator.middleware import PrometheusInstrumentatorMiddleware

    global _requests, _in_progress
    if _requests is None:
        _requests = request_metrics.default(
            should_exclude_streaming_duration=True,  # time to the first byte: see the module's docstring
            latency_lowr_buckets=REQUEST_BUCKETS,
        )
        _in_progress = Gauge("http_requests_inprogress", "Number of HTTP requests in progress.", ["method", "handler"])

    class CountsRequests(PrometheusInstrumentatorMiddleware):
        def __init__(self, inner: Any, **options: Any) -> None:
            super().__init__(inner, **options)
            self.should_instrument_requests_inprogress, self.inprogress = True, _in_progress

    app.add_middleware(
        CountsRequests,
        inprogress_labels=True,
        excluded_handlers=["^/metrics$"],  # Prometheus reading the numbers is not traffic
        instrumentations=[_requests],
    )
    Instrumentator().expose(app, include_in_schema=False, dependencies=[Depends(guard)])

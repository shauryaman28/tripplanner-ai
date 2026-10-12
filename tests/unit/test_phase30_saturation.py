"""Phase 30 — what the app says about how near it is to running out of something.

A load test finds where a system stops coping; these three numbers are how it is seen coming, and
they stay in the app after the test is over (GET /metrics):

    db_pool_connections{state}, db_pool_connections_limit        the database pool
    redis_pool_connections{state}, redis_pool_connections_limit  the Redis pool — an open stream holds one
    event_loop_lag_seconds                                       how late the event loop runs what is due

They are read from real pools here — SQLAlchemy's and redis-py's own objects — because what they
report are those libraries' internals, and a library that renames one must fail a test, not flatten
a chart.
"""

import asyncio
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.asyncio as aioredis
import sqlalchemy
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy.pool import QueuePool

from app import main
from app.db import redis as redis_module
from app.db import session
from src.telemetry import metrics


def reading(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels or None)


@pytest.fixture
def the_apps_pools():
    """Whatever a test points the gauges at, the app's own pools are what they read afterwards."""
    yield
    metrics.watch_db_pool(lambda: session.engine.pool)
    metrics.watch_redis_pool(lambda: redis_module.redis_client.connection_pool)


# ── The database pool ──────────────────────────────────────────────────────


def test_the_database_pool_is_reported_as_it_is_when_asked(the_apps_pools):
    engine = sqlalchemy.create_engine("sqlite://", poolclass=QueuePool, pool_size=2, max_overflow=3)
    metrics.watch_db_pool(lambda: engine.pool)

    assert reading("db_pool_connections_limit") == 5  # its size and its overflow: past this a request waits
    assert reading("db_pool_connections", state="in_use") == 0

    lent = [engine.connect() for _ in range(4)]  # two of the pool's own, two of its overflow
    assert reading("db_pool_connections", state="in_use") == 4
    lent.pop().close()
    assert reading("db_pool_connections", state="in_use") == 3
    assert reading("db_pool_connections", state="idle") == 1  # given back, and kept for the next
    for connection in lent:
        connection.close()
    assert reading("db_pool_connections", state="in_use") == 0
    engine.dispose()


def test_the_apps_own_pool_is_what_get_metrics_shows():
    pool = session.engine.pool

    assert reading("db_pool_connections_limit") == pool.size() + pool._max_overflow
    assert reading("db_pool_connections", state="in_use") == pool.checkedout()
    shown = generate_latest(REGISTRY).decode()
    for line in (
        'db_pool_connections{state="in_use"}',
        'db_pool_connections{state="idle"}',
        "db_pool_connections_limit",
    ):
        assert f"\n{line} " in shown, line


def test_importing_the_app_is_enough_for_its_database_pool_to_be_reported():
    """Nobody has to remember to call anything: the module that makes the engine points the gauges at its pool.

    Asked of a fresh interpreter. In this one the tests above have pointed the gauges about, and put
    them back themselves — which is exactly what would hide the app no longer doing it.
    """
    root = Path(__file__).resolve().parents[2]
    told = {
        "DATABASE_URL": "postgresql://tripplanner:pw@localhost:5432/tripplanner_db",
        "DB_POOL_SIZE": "3",
        "DB_MAX_OVERFLOW": "4",
        "APP_ENV": "test",
        "LOG_FORMAT": "text",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        "SENTRY_DSN": "",
        "PYTHONPATH": f"{root / 'src' / 'backend'}:{root}",
        "PATH": "/usr/bin:/bin",
    }
    asks = (
        "from prometheus_client import REGISTRY\n"
        "from app.db import session\n"
        "print(REGISTRY.get_sample_value('db_pool_connections_limit'),\n"
        "      REGISTRY.get_sample_value('db_pool_connections', {'state': 'in_use'}))\n"
    )
    asked = subprocess.run(
        [sys.executable, "-c", asks], env=told, capture_output=True, text=True, cwd=root, timeout=120
    )

    assert asked.returncode == 0, asked.stderr[-600:]
    assert asked.stdout.split() == ["7.0", "0.0"]  # the pool the settings say, nothing lent yet — and not NaN


# ── The Redis pool ─────────────────────────────────────────────────────────


def test_the_redis_pool_is_reported_and_so_is_the_most_it_will_open(the_apps_pools):
    client = aioredis.from_url("redis://localhost:6379", **redis_module.connection_options())
    metrics.watch_redis_pool(lambda: client.connection_pool)

    # Found by reading this number, not by setting it: the library opens a hundred connections and no more
    # — and the app never said how many it wanted. An open event stream keeps one for as long as it is open.
    assert reading("redis_pool_connections_limit") == client.connection_pool.max_connections
    assert reading("redis_pool_connections", state="in_use") == 0

    held = [client.connection_pool.get_available_connection() for _ in range(3)]  # taken, not yet connected
    assert reading("redis_pool_connections", state="in_use") == 3
    assert reading("redis_pool_connections", state="idle") == 0
    assert len(held) == 3


def test_a_pool_that_cannot_be_read_is_no_number_and_never_a_failed_scrape(the_apps_pools):
    """Before the app has a Redis client there is no pool; a library may rename what is read. Neither fails GET /metrics."""
    metrics.watch_redis_pool(lambda: None)
    metrics.watch_db_pool(lambda: object())

    assert math.isnan(reading("redis_pool_connections", state="in_use"))
    assert math.isnan(reading("redis_pool_connections_limit"))
    assert math.isnan(reading("db_pool_connections", state="in_use")) and math.isnan(
        reading("db_pool_connections_limit")
    )
    assert "db_pool_connections_limit NaN" in generate_latest(REGISTRY).decode()  # the scrape itself is whole


async def test_the_redis_client_the_app_makes_is_the_one_reported(the_apps_pools):
    made = MagicMock()
    made.connection_pool._in_use_connections = {object(), object()}
    made.connection_pool._available_connections = [object()]
    made.connection_pool.max_connections = 100

    with (
        patch.object(redis_module.aioredis, "from_url", return_value=made),
        patch.object(redis_module.traces, "instrument_redis"),
    ):
        was = redis_module.redis_client
        # wherever an earlier test left the gauges: making the client is what points them at it
        metrics.watch_redis_pool(lambda: None)
        try:
            await redis_module.init_redis()
            assert reading("redis_pool_connections", state="in_use") == 2
            assert reading("redis_pool_connections", state="idle") == 1
            assert reading("redis_pool_connections_limit") == 100
        finally:
            redis_module.redis_client = was


# ── The event loop ─────────────────────────────────────────────────────────


def lag_observed() -> tuple[float, float]:
    return reading("event_loop_lag_seconds_count"), reading("event_loop_lag_seconds_bucket", le="0.05")


async def test_work_that_does_not_give_the_event_loop_back_shows_as_lateness():
    """A password being hashed takes a fifth of a second and gives nothing else a turn: every other request waits."""
    count, quick = lag_observed()
    watching = asyncio.create_task(metrics.watch_event_loop(every=0.01))
    await asyncio.sleep(0.05)
    idle_count, idle_quick = lag_observed()
    assert idle_count > count and idle_count - count == idle_quick - quick  # nothing in the way: nothing late

    time.sleep(0.2)  # …and then something that blocks
    await asyncio.sleep(0.03)
    watching.cancel()

    late_count, late_quick = lag_observed()
    assert (late_count - idle_count) - (late_quick - idle_quick) >= 1  # one wake-up came more than 50 ms late
    assert reading("event_loop_lag_seconds_bucket", le="0.25") - reading("event_loop_lag_seconds_bucket", le="0.1") >= 1


async def test_lateness_is_how_much_later_than_asked_and_not_the_wait_itself():
    """Asked to be woken in 50 ms and woken in 51: one millisecond late, not fifty-one."""

    def within_25_ms() -> float:
        return reading("event_loop_lag_seconds_bucket", le="0.025")

    before = within_25_ms()
    watching = asyncio.create_task(metrics.watch_event_loop(every=0.05))
    await asyncio.sleep(0.3)
    watching.cancel()

    assert within_25_ms() - before >= 1  # an idle loop wakes it on time; the 50 ms it asked for is not lateness


def test_lateness_is_looked_for_ten_times_a_second_and_told_apart_from_a_millisecond_to_seconds():
    assert metrics.LAG_EVERY_S == 0.1
    assert metrics.LAG_BUCKETS[0] == 0.001 and metrics.LAG_BUCKETS[-1] == 2.5 and 0.25 in metrics.LAG_BUCKETS


async def test_the_app_watches_its_event_loop_for_as_long_as_it_serves():
    watched: list[str] = []

    async def watch() -> None:
        watched.append("started")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            watched.append("stopped")
            raise

    with (
        patch.multiple(
            main,
            _show_app_logs=MagicMock(),
            init_redis=AsyncMock(return_value=AsyncMock()),
            _recover_after_restart=AsyncMock(),
            finish_runs=AsyncMock(return_value=0),
            close_redis=AsyncMock(),
            close_session=AsyncMock(),
        ),
        patch.object(main.metrics, "watch_event_loop", watch),
    ):
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
            assert watched == ["started"]
        await asyncio.sleep(0)

    assert watched == ["started", "stopped"]


# ── On the dashboard ───────────────────────────────────────────────────────


def test_the_dashboard_has_a_row_for_what_runs_out_under_load():
    """Watched live while a load test runs (docs/load-test-report.md): the three numbers, each against its limit."""
    root = Path(__file__).resolve().parents[2]
    dashboard = json.loads((root / "docker/observability/grafana/dashboards/tripplanner.json").read_text())
    panels = dashboard["panels"]
    (row,) = [panel for panel in panels if panel["type"] == "row" and "Under load" in panel["title"]]
    after = panels[panels.index(row) + 1 : panels.index(row) + 4]
    asked = {panel["title"]: " ".join(target["expr"] for target in panel["targets"]) for panel in after}

    assert list(asked) == ["Database connections", "Redis connections and open streams", "Event loop lateness"]
    assert 'db_pool_connections{job=~"$job", state="in_use"}' in asked["Database connections"]
    assert (
        "db_pool_connections_limit" in asked["Database connections"]
    )  # in use means nothing without what it is out of
    streams = asked["Redis connections and open streams"]
    assert "redis_pool_connections_limit" in streams and 'handler="/trips/{trip_id}/stream"' in streams
    assert (
        "histogram_quantile(0.99" in asked["Event loop lateness"]
        and "event_loop_lag_seconds_bucket" in asked["Event loop lateness"]
    )
    # the row sits above the log lines, and nothing overlaps it
    assert [panel["gridPos"]["y"] for panel in after] == [row["gridPos"]["y"] + 1] * 3
    assert (
        panels[-1]["type"] == "logs"
        and panels[-1]["gridPos"]["y"] > after[0]["gridPos"]["y"] + after[0]["gridPos"]["h"]
    )

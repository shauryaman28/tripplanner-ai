"""Phase 34 — GET /admin/health-deep, and the embedding queue on the dashboard.

The deep check asks every dependency to do its work once — Postgres a SELECT 1, Redis a PING, the tool
server an estimate_budget (the real server object here, in memory), LangSmith to accept the key — each
with its own deadline, all at once. What is held here: each one's answer, each one's way of failing
(down, refusing, hanging), what is required and what is not, who may ask, and that nothing secret is in
what it says. On real Postgres, Redis and a tool server in a process of its own:
tests/integration/test_phase34_health_integration.py.
"""

import asyncio
import dataclasses
import errno
import logging
import math
import socket
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.core import deep_health
from app.core.config import settings
from app.db import redis as redis_module
from app.db import session as session_module
from app.main import app
from src.ai.mcp_server.models import ToolError
from src.telemetry import metrics
from tests.fakes import tool_server
from tests.unit.test_phase32_data_layer import as_sqlalchemy_raises

TOKEN = "an-operations-token-of-thirty-two-chars"
SECRET = "the-redis-password-nobody-may-read-1234"


def a_session(answer=1, fails: Exception | None = None, hangs: bool = False):
    """What AsyncSessionLocal() hands out: a session whose SELECT 1 answers, fails or never answers."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    async def execute(_statement):
        if hangs:
            await asyncio.sleep(60)
        if fails:
            raise fails
        result = MagicMock()
        result.scalar.return_value = answer
        return result

    session.execute = execute
    return MagicMock(return_value=session)


def a_redis(pong=True, fails: Exception | None = None):
    client = AsyncMock()
    client.ping = AsyncMock(side_effect=fails) if fails else AsyncMock(return_value=pong)
    return client


class LangSmith:
    """LangSmith's API as httpx sees it: what it answers, and what it was asked."""

    def __init__(self, status: int = 200, fails: Exception | None = None) -> None:
        self.status, self.fails, self.asked = status, fails, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        if self.fails:
            raise self.fails
        return httpx.Response(self.status, json=[] if self.status == 200 else {"detail": "Forbidden"})

    def client(self):
        transport = httpx.MockTransport(self)
        return lambda: httpx.AsyncClient(transport=transport)


async def ask(
    headers: dict | None = None, *, postgres=None, redis=None, langsmith: LangSmith | None = None, **settings_
):
    """GET /admin/health-deep with these dependencies — the tool server always the real one, in memory."""
    langsmith = langsmith or LangSmith()
    with (
        patch.object(session_module, "AsyncSessionLocal", postgres or a_session()),
        patch.object(redis_module, "redis_client", redis if redis is not None else a_redis()),
        patch.object(deep_health, "_langsmith_client", langsmith.client()),
        # every setting it reads, said here — not left to whatever a developer's .env holds
        patch.multiple(
            settings,
            **{
                "APP_ENV": "development",
                "OPS_TOKEN": "",
                "LANGCHAIN_API_KEY": "",
                "LANGCHAIN_ENDPOINT": "",
                **settings_,
            },
        ),
    ):
        async with tool_server():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                return await client.get("/admin/health-deep", headers=headers or {})


# ── Every dependency, working ──────────────────────────────────────────────


async def test_with_everything_working_each_dependency_says_what_it_did():
    langsmith = LangSmith()
    answered = await ask(langsmith=langsmith, LANGCHAIN_API_KEY="lsv2-a-key")

    assert answered.status_code == 200
    body = answered.json()
    assert body["status"] == "ok" and set(body) == {"status", "checked_at", "took_ms", "dependencies"}
    found = body["dependencies"]
    assert list(found) == ["postgres", "redis", "tool_server", "langsmith"]
    assert found["postgres"] == {**found["postgres"], "status": "ok", "required": True, "detail": "SELECT 1 answered"}
    assert found["redis"]["status"] == "ok" and found["redis"]["detail"] == "PING answered"
    # the real estimate_budget, through the real client and server: arithmetic, no provider
    assert found["tool_server"]["status"] == "ok"
    assert found["tool_server"]["detail"].startswith("estimate_budget answered: ₹")
    assert found["langsmith"] == {
        **found["langsmith"],
        "status": "ok",
        "required": False,
        "detail": "the key is accepted · tracing off (LANGCHAIN_TRACING_V2 is not true)",
    }
    assert all(isinstance(result["latency_ms"], float) for result in found.values())
    (request,) = langsmith.asked
    assert str(request.url) == "https://api.smith.langchain.com/api/v1/sessions?limit=1"
    assert request.headers["x-api-key"] == "lsv2-a-key"


async def test_another_langsmith_is_asked_where_the_settings_say():
    langsmith = LangSmith()
    await ask(langsmith=langsmith, LANGCHAIN_API_KEY="local", LANGCHAIN_ENDPOINT="http://localhost:1984/")
    assert str(langsmith.asked[0].url) == "http://localhost:1984/api/v1/sessions?limit=1"


async def test_the_tool_servers_answer_is_the_budget_it_was_asked_for():
    from src.ai.mcp_server.models import BudgetInput
    from src.ai.mcp_server.tools import estimate_budget

    expected = estimate_budget(BudgetInput(**deep_health.BUDGET_QUESTION)).total
    answered = (await ask()).json()["dependencies"]["tool_server"]
    assert answered["detail"] == f"estimate_budget answered: ₹{expected:,.0f} for the test trip"


# ── Each way of failing ────────────────────────────────────────────────────


async def test_postgres_away_is_failing_and_a_503_and_the_others_are_still_asked():
    answered = await ask(postgres=a_session(fails=ConnectionRefusedError("connection refused")))

    assert answered.status_code == 503
    body = answered.json()
    assert body["status"] == "failing"
    assert body["dependencies"]["postgres"]["status"] == "failing"
    assert body["dependencies"]["postgres"]["detail"] == "ConnectionRefusedError: connection refused"
    assert {name: result["status"] for name, result in body["dependencies"].items()} == {
        "postgres": "failing",
        "redis": "ok",
        "tool_server": "ok",
        "langsmith": "off",
    }


async def test_a_select_1_that_answers_wrong_is_failing_too():
    body = (await ask(postgres=a_session(answer=0))).json()
    assert body["dependencies"]["postgres"]["detail"] == "RuntimeError: SELECT 1 answered 0"


@pytest.mark.parametrize(
    ("redis", "said"),
    [
        (a_redis(fails=ConnectionError("Error 111 connecting to redis:6379")), "ConnectionError: Error 111 connecting"),
        (a_redis(pong=False), "RuntimeError: PING answered nothing"),
    ],
)
async def test_redis_away_or_silent_is_failing(redis, said):
    answered = await ask(redis=redis)
    assert answered.status_code == 503
    assert answered.json()["dependencies"]["redis"]["detail"].startswith(said)


async def test_no_redis_client_at_all_is_failing():
    """A backend whose Redis was away at startup has no client: that is the check failing, not the endpoint."""
    with patch.object(redis_module, "redis_client", None):
        found = await deep_health._one(deep_health.Check("redis", True, 1.0, deep_health._redis), str)
    assert found["status"] == "failing" and "Redis not initialised" in found["detail"]


async def test_a_tool_server_that_cannot_be_reached_is_failing_with_the_reason_it_gave():
    gone = ToolError(error="The tool server could not be reached.", code="CONNECTION_REFUSED")
    with patch("src.ai.mcp_client.client.call_tool", AsyncMock(return_value=gone)):
        answered = await ask()

    assert answered.status_code == 503
    assert answered.json()["dependencies"]["tool_server"]["detail"] == (
        "RuntimeError: CONNECTION_REFUSED: The tool server could not be reached."
    )


async def test_a_tool_server_answer_without_a_total_is_not_taken_for_working():
    with patch("src.ai.mcp_client.client.call_tool", AsyncMock(return_value={"notes": "?"})):
        found = (await ask()).json()["dependencies"]["tool_server"]
    assert found["status"] == "failing" and "without a total" in found["detail"]


async def test_a_dependency_that_hangs_is_reported_as_hanging_and_the_answer_still_comes():
    quick = [dataclasses.replace(check, timeout_s=0.3) for check in deep_health.checks(settings)]
    started = time.monotonic()
    with patch.object(deep_health, "checks", lambda *_a, **_k: quick):
        answered = await ask(postgres=a_session(hangs=True))
    took = time.monotonic() - started

    assert answered.status_code == 503
    postgres = answered.json()["dependencies"]["postgres"]
    assert postgres["detail"] == "no answer within 0.3 s" and postgres["latency_ms"] >= 290
    assert took < 5  # not the minute it would have waited


async def test_the_checks_are_asked_together_not_one_after_another():
    async def slow() -> str:
        await asyncio.sleep(0.3)
        return "answered"

    every = [deep_health.Check(f"dep{n}", True, 2.0, slow) for n in range(4)]
    started = time.monotonic()
    found = await deep_health.report(settings, str, every)
    assert time.monotonic() - started < 0.9 and found["status"] == "ok"  # four 0.3 s checks: well under 1.2 s


# ── What is required, and what is not ──────────────────────────────────────


async def test_langsmith_refusing_the_key_is_degraded_and_still_200_plans_are_made_without_it():
    answered = await ask(langsmith=LangSmith(status=403), LANGCHAIN_API_KEY="lsv2-revoked")

    assert answered.status_code == 200
    body = answered.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["langsmith"] == {
        **body["dependencies"]["langsmith"],
        "status": "failing",
        "required": False,
        "detail": "RuntimeError: the key was refused (HTTP 403)",
    }


@pytest.mark.parametrize(
    ("langsmith", "said"),
    [
        (LangSmith(fails=httpx.ConnectError("name resolution failed")), "ConnectError: name resolution failed"),
        (LangSmith(status=502), "RuntimeError: HTTP 502 from https://api.smith.langchain.com"),
    ],
)
async def test_langsmith_out_of_reach_is_degraded(langsmith, said):
    body = (await ask(langsmith=langsmith, LANGCHAIN_API_KEY="lsv2-a-key")).json()
    assert body["status"] == "degraded" and body["dependencies"]["langsmith"]["detail"] == said


@pytest.mark.parametrize(
    ("setting", "said"),
    [
        ({"LANGCHAIN_API_KEY": ""}, "no LANGCHAIN_API_KEY: traces are not sent"),
        ({"LANGCHAIN_API_KEY": "lsv2-a-key", "APP_ENV": "test"}, "APP_ENV is test: LangSmith is never called"),
    ],
)
async def test_langsmith_not_set_up_is_off_not_failing_and_is_not_called(setting, said):
    langsmith = LangSmith()
    body = (await ask(langsmith=langsmith, **setting)).json()

    assert body["status"] == "ok"
    assert body["dependencies"]["langsmith"] == {"status": "off", "required": False, "latency_ms": None, "detail": said}
    assert langsmith.asked == []


async def test_langsmith_tracing_on_is_said_with_its_project():
    with patch("src.ai.tracing.enabled", return_value=True):
        body = (await ask(LANGCHAIN_API_KEY="lsv2-a-key", LANGCHAIN_PROJECT="tripplanner-ai")).json()
    assert body["dependencies"]["langsmith"]["detail"] == "the key is accepted · tracing on, project 'tripplanner-ai'"


def test_the_overall_status_is_the_worst_of_what_is_required():
    def found(*results):
        return {f"d{n}": {"status": status, "required": required} for n, (status, required) in enumerate(results)}

    assert deep_health.overall(found(("ok", True), ("off", False))) == "ok"
    assert deep_health.overall(found(("ok", True), ("failing", False))) == "degraded"
    assert deep_health.overall(found(("failing", True), ("ok", False))) == "failing"
    assert deep_health.overall(found(("failing", True), ("failing", False))) == "failing"


# ── Who may ask ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("app_env", "token", "sent", "status"),
    [
        ("development", "", {}, 200),  # a laptop: open, as GET /metrics is
        ("production", "", {}, 404),  # a deployment that forgot its token answers nobody
        ("production", "", {"Authorization": "Bearer anything"}, 404),
        ("production", TOKEN, {}, 404),
        ("production", TOKEN, {"Authorization": "Bearer wrong-token"}, 404),
        ("production", TOKEN, {"Authorization": f"Bearer {TOKEN}"}, 200),
        ("production", TOKEN, {"X-Ops-Token": TOKEN}, 200),
        ("development", TOKEN, {}, 404),  # a token set is a token required, wherever
    ],
)
async def test_only_the_operator_may_ask(app_env, token, sent, status):
    answered = await ask(sent, APP_ENV=app_env, OPS_TOKEN=token)
    assert answered.status_code == status
    if status == 404:
        assert "dependencies" not in answered.text  # nothing about the dependencies to whoever may not ask


def test_a_deployment_without_an_operations_token_is_told_the_deep_check_answers_nobody():
    from app import preflight
    from tests.unit.test_phase27_production import deployment, providers

    report = preflight.check(deployment(OPS_TOKEN=""), providers())
    assert report.ok  # it starts…
    assert "GET /admin/health-deep answers nobody either, so its dependencies cannot be asked" in report.text()
    assert "health-deep" not in preflight.check(deployment(), providers()).text()  # …and with a token, nothing to say


def test_it_is_documented_as_the_operators_and_not_a_platforms_health_check():
    from app.api.deps import may_check_health, may_read_metrics

    assert may_check_health is may_read_metrics
    paths = set(app.openapi()["paths"])
    assert {"/admin/health-deep", "/ready", "/ping"} <= paths


# ── What it says ───────────────────────────────────────────────────────────


async def test_nothing_secret_is_in_what_a_failing_check_says(caplog):
    """A connection error names the address it could not reach — with the password in it."""
    from app.api.routes import admin
    from app.core.error_reporting import Scrubber

    caplog.set_level(logging.INFO, logger="app.core.deep_health")
    failing = a_redis(fails=ConnectionError(f"Error connecting to redis://default:{SECRET}@redis.internal:6379/0"))
    with patch.object(admin, "_scrub", Scrubber([SECRET]).text):
        answered = await ask(redis=failing)

    assert SECRET not in answered.text and "[Filtered]" in answered.json()["dependencies"]["redis"]["detail"]
    assert SECRET not in caplog.text and "[HEALTH] redis is failing" in caplog.text


def test_the_route_scrubs_with_every_secret_the_settings_hold():
    from app.api.routes import admin

    assert settings.JWT_SECRET and settings.JWT_SECRET not in admin._scrub(f"signed with {settings.JWT_SECRET}")


async def test_a_required_dependency_failing_is_a_warning_in_the_log_and_langsmith_only_a_line(caplog):
    caplog.set_level(logging.INFO, logger="app.core.deep_health")
    await ask(postgres=a_session(fails=OSError("no route")), langsmith=LangSmith(status=403), LANGCHAIN_API_KEY="k")

    levels = {
        record.getMessage().split(" is failing")[0]: record.levelname
        for record in caplog.records
        if "[HEALTH]" in record.getMessage()
    }
    assert levels == {"[HEALTH] postgres": "WARNING", "[HEALTH] langsmith": "INFO"}


# ── The embedding queue, for the dashboard ─────────────────────────────────


def gauge(state: str) -> float:
    return metrics.EMBEDDING_QUEUE.labels(state)._value.get()


async def test_the_embedding_queue_is_counted_every_so_often_and_said_as_no_data_when_it_cannot_be():
    answers = iter(
        [
            {"in_flight": 2, "pending_retry": 5},
            RuntimeError("the database is away"),
            {"in_flight": 0, "pending_retry": 3},
        ]
    )
    seen = []

    async def count():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def look():
        seen.append((gauge("in_flight"), gauge("pending_retry")))

    task = asyncio.create_task(metrics.watch_embedding_queue(count, every=0.05))
    for _ in range(3):
        await asyncio.sleep(0.03)
        await look()
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen[0] == (2.0, 5.0)
    assert all(math.isnan(value) for value in seen[1])  # not known is not empty
    assert seen[2] == (0.0, 3.0)  # and it goes on after a failure


async def test_the_queue_the_gauge_counts_is_what_get_admin_embedding_health_says():
    from app import main
    from app.api.routes import admin

    db, redis = AsyncMock(), AsyncMock()
    redis.get = AsyncMock(return_value=b"4")
    db.scalar = AsyncMock(return_value=7)
    assert await admin.embedding_backlog(db, redis) == {"in_flight": 4, "pending_retry": 7}
    redis.get = AsyncMock(return_value=b"-2")  # a counter left below zero by an instance that restarted
    db.scalar = AsyncMock(return_value=None)
    assert await admin.embedding_backlog(db, redis) == {"in_flight": 0, "pending_retry": 0}

    with (
        patch.object(main, "AsyncSessionLocal", a_session()),
        patch.object(main.redis_db, "redis_client", redis),
        patch.object(main, "embedding_backlog", AsyncMock(return_value={"in_flight": 1, "pending_retry": 2})) as asked,
    ):
        assert await main._embedding_queue() == {"in_flight": 1, "pending_retry": 2}
    assert asked.await_args.args[1] is redis


async def test_the_app_counts_the_queue_while_it_runs_and_stops_when_it_stops():
    from app import main

    started = []

    async def watch(count, every=15.0):
        started.append(count)
        await asyncio.sleep(3600)

    with (
        patch.object(main.metrics, "watch_embedding_queue", watch),
        patch.multiple(
            main,
            _show_app_logs=MagicMock(),
            init_redis=AsyncMock(return_value=AsyncMock()),
            _recover_after_restart=AsyncMock(),
            close_redis=AsyncMock(),
            close_session=AsyncMock(),
            finish_runs=AsyncMock(return_value=0),
        ),
    ):
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
            assert started == [main._embedding_queue]
            running = [task for task in asyncio.all_tasks() if task.get_coro().__name__ == "watch"]
            assert len(running) == 1
        await asyncio.sleep(0)
        assert running[0].cancelled() or running[0].done()


async def test_get_admin_embedding_health_still_answers_as_it_did():
    from app.api.deps import get_current_user, get_redis_dep
    from app.db.session import get_db

    db, redis = AsyncMock(), AsyncMock()
    redis.get = AsyncMock(return_value=b"1")
    db.scalar = AsyncMock(return_value=2)
    app.dependency_overrides.update(
        {get_current_user: lambda: MagicMock(), get_db: lambda: db, get_redis_dep: lambda: redis}
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            answered = await client.get("/admin/embedding-health")
    finally:
        app.dependency_overrides.clear()
    assert answered.json() == {"status": "degraded", "in_flight": 1, "pending_retry": 2}


# ── Found by the alert drill: a database whose name does not resolve ────────
#
# `python scripts/alert_drill.py errors` stops the stack's pooler. Its records had every request answered 500,
# where Phase 32 had made "the database cannot be reached" a 503: with the container gone its name no longer
# resolved, and asyncpg raised socket.gaierror as it was — an OSError, not a ConnectionError, which the
# classifier and the handler had never met. A run that could not mark its trip gave up at once, too.

GAI = socket.gaierror(-2, "Name or service not known")  # what the backend logged, word for word


@pytest.mark.parametrize(
    "away",
    [
        GAI,
        as_sqlalchemy_raises(GAI),
        OSError(errno.ENETUNREACH, "Network is unreachable"),
        OSError(errno.EHOSTUNREACH, "No route to host"),
    ],
)
def test_a_database_whose_name_does_not_resolve_or_cannot_be_routed_to_is_away(away):
    from app.db.errors import database_is_away

    assert database_is_away(away)


@pytest.mark.parametrize(
    "not_away",
    [FileNotFoundError(2, "No such file or directory"), PermissionError(13, "Permission denied"), OSError("?")],
)
def test_other_os_errors_are_not_the_database(not_away):
    from app.db.errors import database_is_away

    assert not database_is_away(not_away)


async def _a_request_whose_database(raises: BaseException):
    from app.api.deps import get_current_user
    from app.db.session import get_db

    async def database():
        raise raises
        yield  # a dependency that gives up its session as the database's driver would

    app.dependency_overrides.update({get_current_user: lambda: MagicMock(), get_db: database})
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
        ) as http:
            return await http.get("/trips")
    finally:
        app.dependency_overrides.clear()


async def test_a_request_that_cannot_resolve_its_database_is_told_to_come_back():
    answered = await _a_request_whose_database(GAI)
    assert answered.status_code == 503 and answered.headers["retry-after"] == "5"
    assert answered.json()["error"]["code"] == "SERVICE_UNAVAILABLE" and "gaierror" not in answered.text
    from app import main

    assert main.app.exception_handlers[OSError] is main.database_is_away_handler


async def test_any_other_os_error_is_still_the_500_it_was():
    answered = await _a_request_whose_database(FileNotFoundError(2, "No such file or directory"))
    assert answered.status_code == 500
    assert answered.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"


async def test_a_run_that_cannot_resolve_its_database_goes_on_trying_to_mark_its_trip():
    from app.api.routes import trips
    from app.models.trip import TripStatus
    from tests.unit.test_phase32_data_layer import a_database

    trip = MagicMock(status=TripStatus.PLANNING)
    factory, seen = a_database(trip, away_for=3, then=GAI)
    with patch.object(trips, "AsyncSessionLocal", factory), patch.object(trips, "SETTLE_EVERY_S", 0):
        await trips._settle(uuid.uuid4(), 1, TripStatus.FAILED, trips.DATABASE_AWAY)
    assert seen["n"] == 4 and trip.status == TripStatus.FAILED  # tried until it was back, then marked
    assert trips._told(GAI) == trips.DATABASE_AWAY  # and the traveller is told that, not "something went wrong"

"""Phase 27 — what production does to the app, on a real Postgres and a real Redis.

    RUN_INTEGRATION=1 pytest tests/integration/test_phase27_production_integration.py -v

The unit tests hold the rules; these hold that the rules are true of the real
libraries talking to real servers:

- a URL written the way a hosted Postgres hands it out connects, through both drivers;
- an instance that starts leaves alone a trip another instance is planning, fails one whose
  run died, and notices later when the other instance dies after all;
- a run given up because the server is stopping can be started again, and says why it failed;
- a Redis connection closed by the server costs the next command nothing, and a subscription
  goes on hearing;
- an error report really arrives at a server over HTTP, and has no secret in it.

The services here are the development ones (no TLS, no pooler). What TLS, a pooler in
transaction mode and a stop signal do is the production-like stack's to show —
docker/prodlike, scripts/prod_check.py, docs/phase27_build_log.md.
"""

import asyncio
import importlib.util
import io
import os
import sys
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import redis.asyncio as aioredis
import sentry_sdk
from alembic import command
from alembic.config import Config
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import select

from app import main
from app.api.routes import trips as trips_module
from app.api.routes.trips import INTERRUPTED, finish_runs
from app.core import error_reporting
from app.core.config import settings
from app.db import redis as redis_db
from app.db.url import async_engine_arguments, sync_url
from app.models.agent_run import AgentRun
from app.models.itinerary import Itinerary
from app.models.trip import Trip, TripStatus
from app.models.user import User
from src.ai.utils.conversation import clear_current_run, run_in_flight, save_current_run
from tests.fakes import flight
from tests.integration.test_pipeline_integration import _create_trip, _login, _run_finished, _stack, _until
from tests.unit.test_phase27_production import GOOD, PROVIDERS, deployment, providers

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

# scripts/ is not a package: load the receiver the way `python scripts/sentry_receiver.py` runs it.
_spec = importlib.util.spec_from_file_location(
    "sentry_receiver", Path(__file__).resolve().parents[2] / "scripts" / "sentry_receiver.py"
)
sentry_receiver = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sentry_receiver
_spec.loader.exec_module(sentry_receiver)

START = date.today() + timedelta(days=30)


def as_handed_out(database_url: str) -> str:
    """This test database's URL with a hosted Postgres's parameters on it. `prefer`, not `require`: the
    development server has no certificate — the parameters still have to survive both drivers."""
    return (
        make_url(database_url)
        .set(
            query={
                "sslmode": "prefer",
                "channel_binding": "prefer",
                "application_name": "phase27-test",
                "connect_timeout": "5",
            }
        )
        .render_as_string(hide_password=False)
    )


# ── The database URL, on a real server ─────────────────────────────────────


async def test_a_url_with_a_hosted_postgress_parameters_connects_through_both_drivers(db_session):
    handed_out = as_handed_out(settings.DATABASE_URL)

    url, connect_args = async_engine_arguments(handed_out)
    engine = create_async_engine(url, connect_args=connect_args, pool_pre_ping=True)
    try:
        names = set()
        for n in range(12):  # the same statement in a dozen transactions, nothing prepared kept between them
            async with engine.connect() as db:
                names.add(await db.scalar(text("SELECT current_setting('application_name')")))
                assert await db.scalar(text("SELECT :n + 1"), {"n": n}) == n + 1
        assert names == {"phase27-test"}  # what was asked for in the URL reached the server
    finally:
        await engine.dispose()

    alembics = create_engine(sync_url(handed_out))  # psycopg2 reads the very same URL
    with alembics.connect() as db:
        assert db.scalar(text("SELECT current_setting('application_name')")) == "phase27-test"
    alembics.dispose()


def test_migrations_go_to_the_direct_address_when_one_is_given():
    """A pooler in transaction mode is the wrong road for DDL. With DATABASE_URL_UNPOOLED set, Alembic uses it —
    shown here by making the pooled address one that leads nowhere."""
    nowhere = make_url(settings.DATABASE_URL).set(database="no_such_database_p27").render_as_string(hide_password=False)

    with patch.multiple(settings, DATABASE_URL=nowhere, DATABASE_URL_UNPOOLED=as_handed_out(settings.DATABASE_URL)):
        command.upgrade(Config("alembic.ini"), "head")  # at head already: it only has to get there

    with patch.multiple(settings, DATABASE_URL=nowhere, DATABASE_URL_UNPOOLED=""):
        with pytest.raises(Exception, match="no_such_database_p27"):
            command.upgrade(Config("alembic.ini"), "head")


# ── An instance that starts beside another ─────────────────────────────────


async def a_user(db) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
    db.add(user)
    await db.commit()
    return user


async def trips_in(db, user: User, *statuses: TripStatus) -> list[Trip]:
    made = [
        Trip(
            user_id=user.id,
            destination="Goa",
            start_date=START,
            end_date=START + timedelta(days=2),
            budget=50_000,
            status=s,
        )
        for s in statuses
    ]
    db.add_all(made)
    await db.commit()
    return made


async def test_a_starting_instance_fails_the_runs_that_died_and_leaves_alone_the_one_still_being_planned(db_session):
    user = await a_user(db_session)
    alive, died, refinement, untouched = await trips_in(
        db_session, user, TripStatus.PLANNING, TripStatus.PLANNING, TripStatus.PLANNING, TripStatus.PENDING
    )
    db_session.add(Itinerary(trip_id=refinement.id, structured_data={"days": []}, total_cost=0.0))
    db_session.add(
        AgentRun(trip_id=died.id, agent_name="flight_agent", status="completed", turn=2, input={}, output={})
    )
    await db_session.commit()

    redis = await redis_db.init_redis()
    later: list = []
    try:
        await save_current_run(redis, str(alive.id), {"turn": 1})  # another instance is at work on this one
        with patch.object(main, "spawn", side_effect=lambda job: (later.append(job.cr_code.co_name), job.close())):
            await main._recover_after_restart()
    finally:
        await clear_current_run(redis, str(alive.id))
        await redis_db.close_redis()

    for row in (alive, died, refinement, untouched):
        await db_session.refresh(row)
    assert (alive.status, died.status, refinement.status, untouched.status) == (
        TripStatus.PLANNING,  # left alone: its run's note was there
        TripStatus.FAILED,  # nobody was planning it any more
        TripStatus.COMPLETED,  # an interrupted change leaves the plan that stood
        TripStatus.PENDING,
    )
    assert later == ["_look_again"]  # and the one left alone is not forgotten
    reasons = (await db_session.execute(select(AgentRun).where(AgentRun.agent_name == "interrupted"))).scalars().all()
    (reason,) = reasons  # only for the trip that failed: a change that was interrupted has a plan to show
    assert (reason.trip_id, reason.status, reason.turn, reason.output) == (
        died.id,
        "failed",
        2,
        {"reason": INTERRUPTED},
    )


async def test_an_instance_that_dies_after_all_is_noticed_by_the_one_that_left_its_trip_alone(db_session):
    user = await a_user(db_session)
    dies_later, finishes = await trips_in(db_session, user, TripStatus.PLANNING, TripStatus.PLANNING)

    redis = await redis_db.init_redis()
    try:
        for trip in (dies_later, finishes):
            await save_current_run(redis, str(trip.id), {"turn": 1})
        with patch.object(main, "RUN_TTL_S", 0.05):  # looked at again every 50 ms instead of every 45 s
            looking = asyncio.create_task(main._look_again([dies_later.id, finishes.id]))
            await asyncio.sleep(0.15)
            for trip in (dies_later, finishes):
                await db_session.refresh(trip)
            assert (dies_later.status, finishes.status) == (
                TripStatus.PLANNING,
                TripStatus.PLANNING,
            )  # both still alive

            finishes.status = TripStatus.COMPLETED  # one run ends as runs do…
            await db_session.commit()
            await clear_current_run(redis, str(finishes.id))
            await clear_current_run(redis, str(dies_later.id))  # …the other's note just stops being renewed
            await asyncio.wait_for(looking, timeout=5)  # and then there is nothing left to look at
    finally:
        await redis_db.close_redis()

    for trip in (dies_later, finishes):
        await db_session.refresh(trip)
    assert (dies_later.status, finishes.status) == (TripStatus.FAILED, TripStatus.COMPLETED)
    (reason,) = (await db_session.execute(select(AgentRun).where(AgentRun.agent_name == "interrupted"))).scalars().all()
    assert reason.trip_id == dies_later.id


# ── A server that stops in the middle of a plan ────────────────────────────


async def test_a_plan_given_up_by_a_stopping_server_says_why_and_can_be_started_again(db_session):
    """Over HTTP, with the real background run: before, the trip stayed "planning" and every later request was a 409."""
    searching = asyncio.Event()

    async def a_search_that_never_returns(*_):
        searching.set()
        await asyncio.sleep(3600)

    answers = iter([a_search_that_never_returns])

    async def flights(*args):
        slow = next(answers, None)
        return await slow(*args) if slow else [flight(8_200.0)]

    async with _stack(flight_tool=flights) as (client, events, _tools):
        await _login(client)
        trip_id = await _create_trip(client)
        assert (await client.post(f"/trips/{trip_id}/plan")).status_code == 202
        await asyncio.wait_for(searching.wait(), timeout=10)
        assert await run_in_flight(await redis_db.get_redis(), trip_id)  # the run's sign of life, in the real Redis
        assert (await client.post(f"/trips/{trip_id}/plan")).status_code == 409  # one run a trip, as ever

        given_up = await finish_runs(grace_seconds=0.1)  # the server is stopping, and this plan is not done

        assert given_up == 1 and not trips_module._runs
        status = (await client.get(f"/trips/{trip_id}/status")).json()
        assert (status["status"], status["failure_reason"]) == ("failed", INTERRUPTED)
        assert not await run_in_flight(await redis_db.get_redis(), trip_id)
        await _until(lambda: _heard(events, "planning_failed"), "the page being told")
        assert [e["error"] for e in events if e.get("event") == "planning_failed"] == [INTERRUPTED]
        timeline = (await client.get(f"/trips/{trip_id}/timeline")).json()
        assert "interrupted" in [entry["agent_name"] for entry in timeline]

        # …and the traveller does what the message says
        assert (await client.post(f"/trips/{trip_id}/plan")).status_code == 202
        runs = await _run_finished(client, trip_id, orchestrator_rows=1)
        assert (await client.get(f"/trips/{trip_id}")).json()["status"] == "completed"
        assert [run["agent_name"] for run in runs].count("interrupted") == 1


async def _heard(events: list[dict], kind: str) -> bool:
    return any(event.get("event") == kind for event in events)


async def test_ready_answers_with_a_status_on_the_real_services(db_session):
    async with _stack(flight_tool=lambda *_: []) as (client, _events, _tools):
        ready = await client.get("/ready")
        assert (ready.status_code, ready.json()) == (200, {"postgres": "ok", "redis": "ok"})
        assert ready.headers["cache-control"] == "no-store"

        await redis_db.close_redis()  # Redis is gone from under the app
        down, ping = await client.get("/ready"), await client.get("/ping")
        assert down.status_code == 503 and down.json()["postgres"] == "ok" and down.json()["redis"].startswith("error")
        assert ping.status_code == 200 and ping.json() == down.json()
        await redis_db.init_redis()


# ── A Redis that closes connections ────────────────────────────────────────


async def kill_connections_named(name: str, kind: str) -> int:
    """Close, in Redis, the connections of one client of ours — and nobody else's on this server."""
    admin = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        ours = [c for c in await admin.client_list(_type=kind) if c.get("name") == name]
        for connection in ours:
            await admin.client_kill_filter(_id=connection["id"])
        return len(ours)
    finally:
        await admin.aclose()


async def test_a_connection_closed_by_redis_costs_the_next_command_nothing():
    name = f"p27-{uuid.uuid4().hex[:8]}"
    as_the_app = aioredis.from_url(settings.REDIS_URL, client_name=name, **redis_db.connection_options())
    by_default = aioredis.from_url(settings.REDIS_URL, client_name=name, decode_responses=True)
    try:
        assert await as_the_app.ping() and await by_default.ping()
        assert (
            await kill_connections_named(name, "normal") == 2
        )  # what a hosted Redis does when its proxy is redeployed

        with pytest.raises(
            RedisConnectionError
        ):  # the library as it comes: one failed request for every closed connection
            await by_default.ping()
        assert await as_the_app.ping() is True  # the app's: sent again on a new connection
        await as_the_app.set(f"{name}:k", "v", ex=5)
        assert await as_the_app.get(f"{name}:k") == "v"
    finally:
        await as_the_app.delete(f"{name}:k")
        await as_the_app.aclose()
        await by_default.aclose()


async def test_a_subscription_whose_connection_is_closed_goes_on_hearing():
    """What the event stream stands on: the page's stream stays open while Redis's connection under it is replaced."""
    name, channel = f"p27-{uuid.uuid4().hex[:8]}", f"p27:{uuid.uuid4().hex}"
    client = aioredis.from_url(settings.REDIS_URL, client_name=name, **redis_db.connection_options())
    subscription = client.pubsub()

    async def heard(wait: float = 3.0) -> str | None:
        deadline = asyncio.get_running_loop().time() + wait
        while asyncio.get_running_loop().time() < deadline:
            message = await subscription.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if message and message["type"] == "message":
                return message["data"]
        return None

    try:
        await subscription.subscribe(channel)
        assert await heard(0.3) is None  # subscribed, and quiet
        await client.publish(channel, "before")
        assert await heard() == "before"

        assert await kill_connections_named(name, "pubsub") == 1
        assert await heard(0.6) is None  # the waits in between are where it connects and subscribes again
        await client.publish(channel, "after")
        assert await heard() == "after"
    finally:
        await subscription.aclose()
        await client.aclose()


# ── An error report, over HTTP ─────────────────────────────────────────────


async def test_an_error_report_arrives_at_a_server_and_has_no_secret_in_it():
    """The roadmap's "appears in Sentry", as far as it goes without Sentry: the SDK's own transport, real
    sockets, and a server that reads what it is sent (scripts/sentry_receiver.py)."""
    printed = io.StringIO()
    receiver = sentry_receiver.Receiver(out=printed)
    server = sentry_receiver.serve(0, receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    marker = f"find-{uuid.uuid4().hex[:8]}"
    secrets = [GOOD["GROQ_API_KEY"], GOOD["JWT_SECRET"], "made-up-db-passw0rd", PROVIDERS["DUFFEL_ACCESS_TOKEN"]]

    try:
        on = error_reporting.configure(
            deployment(SENTRY_DSN=f"http://standin@127.0.0.1:{server.server_port}/1"), providers()
        )
        assert on and error_reporting.enabled()
        try:
            raise RuntimeError(f"{marker}: could not reach {GOOD['DATABASE_URL']} with {' '.join(secrets)}")
        except RuntimeError:
            sentry_sdk.capture_exception()
        with error_reporting.run_scope("the-trip", turn=2):
            sentry_sdk.capture_message(f"{marker}: a run went wrong")
        await asyncio.to_thread(sentry_sdk.flush, 10)
        found = await _until(lambda: _both(receiver, marker), "both reports arriving")
    finally:
        error_reporting.switch_off()
        server.shutdown()

    crash, run = sorted((event["payload"] for event in found), key=lambda p: "exception" not in p)
    assert crash["exception"]["values"][-1]["type"] == "RuntimeError"
    assert (crash["environment"], crash["release"]) == ("production", error_reporting.release())
    assert run["tags"] == {"trip_id": "the-trip", "turn": "2"}
    everything_sent = "\n".join(receiver.raw)
    assert marker in everything_sent and not [secret for secret in secrets if secret in everything_sent]
    assert "[Filtered]@ep-cool-name-pooler" in everything_sent  # which database, without the way in
    assert f"✗ RuntimeError: {marker}" in printed.getvalue()  # and it says so, one line a report


async def _both(receiver, marker: str) -> list | None:
    found = receiver.matching(marker, kinds=("event",))
    return found if len(found) == 2 else None

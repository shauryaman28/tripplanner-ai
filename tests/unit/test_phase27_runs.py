"""Phase 27 — a planning run, when the server under it is replaced.

A plan is a task of one server process. On a laptop that process is the only
one there is; deployed, it is replaced on every deploy, and for a while the old
one and the new one are both alive:

- the new one starts while the old one is still planning — and at startup an instance fails
  every trip it finds "planning", taking them for runs that died. They have not died yet;
- then the platform stops the old one — and a run cut off in the middle used to leave its
  trip "planning" for good: the code that tidies up after a crash did not run for a stop.

So a run keeps a note in Redis for as long as it lives (others leave its trip alone), a
stopping server waits for its runs and gives up the ones that are not done in time, and a
run that is given up says so — to the page, and in the trip's record.

Here with a database and a Redis that are objects in memory; the same against the real
ones in tests/integration/test_phase27_production_integration.py, and with real processes
being stopped and killed on the production-like stack (docs/phase27_build_log.md).
"""

import asyncio
import json
import logging
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app import main
from app.api.routes import trips
from app.api.routes.trips import INTERRUPTED, _keep_noted, _run_orchestrator, finish_runs
from app.models.agent_run import AgentRun
from app.models.trip import TripStatus
from src.ai.utils import conversation
from src.ai.utils.conversation import RUN_BEAT_S, RUN_TTL_S, run_in_flight, save_current_run
from tests.unit.test_phase27_error_reporting import Outbox, reporting  # noqa: F401 — `reporting` is a fixture


class Redis:
    """The few commands a run and its note need, on a dict."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}
        self.sets = 0
        self.published: list[dict] = []

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key], self.ttl[key] = value, ex
        self.sets += 1

    async def delete(self, key):
        self.data.pop(key, None)

    async def exists(self, key):
        return int(key in self.data)

    async def publish(self, _channel, message):
        self.published.append(json.loads(message))


class Database:
    """One trip, and what is added beside it. Every `async with AsyncSessionLocal()` gets this."""

    def __init__(self, status: TripStatus = TripStatus.PLANNING) -> None:
        self.trip = SimpleNamespace(status=status)
        self.added: list = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, _model, _key):
        return self.trip

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


@contextmanager
def a_trip_being_planned():
    database, redis = Database(), Redis()
    with patch.object(trips, "AsyncSessionLocal", database):
        yield database, redis


async def never_finishes(_agent, _db, _hooks) -> dict:
    await asyncio.sleep(3600)
    return {}


async def started(run, redis, **more) -> tuple[asyncio.Task, uuid.UUID]:
    trip_id = uuid.uuid4()
    note = {"turn": more.get("turn", 1)}
    await save_current_run(redis, str(trip_id), note)
    task = asyncio.create_task(_run_orchestrator(trip_id, redis, run, note=note, **more))
    await asyncio.sleep(0.02)  # it is running
    return task, trip_id


# ── The note is the run's sign of life ─────────────────────────────────────


def test_a_note_lives_for_three_beats_so_that_one_missed_beat_changes_nothing():
    assert RUN_TTL_S == 3 * RUN_BEAT_S == 45
    assert trips.RUN_BEAT_S is conversation.RUN_BEAT_S


async def test_a_run_in_flight_keeps_its_note_up_and_a_failing_redis_does_not_stop_it():
    redis, trip_id = Redis(), uuid.uuid4()
    assert await run_in_flight(redis, str(trip_id)) is False

    with patch.object(trips, "RUN_BEAT_S", 0.01):
        beating = asyncio.create_task(_keep_noted(redis, trip_id, {"turn": 2, "refinement_type": "targeted_hotel"}))
        await asyncio.sleep(0.08)
        assert redis.sets >= 3  # again and again
        assert json.loads(redis.data[f"trip:{trip_id}:current_run"]) == {"turn": 2, "refinement_type": "targeted_hotel"}
        assert redis.ttl[f"trip:{trip_id}:current_run"] == RUN_TTL_S
        assert await run_in_flight(redis, str(trip_id)) is True

        redis.set = AsyncMock(side_effect=ConnectionError("redis is away"))  # a beat that fails…
        await asyncio.sleep(0.05)
        assert not beating.done() and redis.set.await_count >= 2  # …is tried again at the next: the run goes on
        beating.cancel()


async def test_a_run_notes_itself_for_as_long_as_it_lasts_and_no_longer():
    with a_trip_being_planned() as (_database, redis), patch.object(trips, "RUN_BEAT_S", 0.01):
        finish = asyncio.Event()

        async def a_plan(_agent, _db, _hooks) -> dict:
            await finish.wait()
            return {"itinerary_id": "saved"}

        task, trip_id = await started(a_plan, redis)
        before = redis.sets
        await asyncio.sleep(0.05)
        assert task in trips._runs  # the server knows it is making this plan
        assert redis.sets > before and await run_in_flight(redis, str(trip_id))  # and so does anyone who asks Redis

        finish.set()
        await task
        assert task not in trips._runs and not await run_in_flight(redis, str(trip_id))
        last = redis.sets
        await asyncio.sleep(0.05)
        assert redis.sets == last  # nothing goes on writing a note for a run that is over


# ── A run that is cut short ────────────────────────────────────────────────


@pytest.mark.parametrize(("has_itinerary", "left_as"), [(False, TripStatus.FAILED), (True, TripStatus.COMPLETED)])
async def test_a_run_cut_short_by_a_stopping_server_does_not_leave_its_trip_planning(has_itinerary, left_as, caplog):
    """Before: only a crash was tidied up after. A cancelled run left the trip "planning", and if the next
    instance had already done its startup recovery, nothing ever reset it — every later request was a 409."""
    with a_trip_being_planned() as (database, redis), caplog.at_level(logging.WARNING, logger="app.api.routes.trips"):
        task, trip_id = await started(never_finishes, redis, turn=2, has_itinerary=has_itinerary)

        task.cancel()  # what a stopping server does to the runs it cannot wait for
        with pytest.raises(asyncio.CancelledError):
            await task

    assert database.trip.status == left_as  # a first plan failed; a change leaves the plan that stood
    (told,) = redis.published
    assert (told["event"], told["status"], told["error"]) == ("planning_failed", "failed", INTERRUPTED)
    (row,) = database.added  # why, for a page loaded afterwards
    assert isinstance(row, AgentRun) and (row.agent_name, row.status, row.turn) == ("interrupted", "failed", 2)
    assert row.output == {"reason": INTERRUPTED} and row.trip_id == trip_id
    assert not await run_in_flight(redis, str(trip_id)) and task not in trips._runs
    assert "was cut short: the server is stopping" in caplog.text
    assert (
        "start it again" in INTERRUPTED and "Nothing is lost" in INTERRUPTED
    )  # said to a traveller, not to an operator


async def test_a_run_that_was_already_over_is_not_marked_when_its_task_is_cancelled_late():
    with a_trip_being_planned() as (database, redis):
        task, _ = await started(never_finishes, redis)
        database.trip.status = TripStatus.COMPLETED  # another instance finished it, or the plan was just saved

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert database.trip.status == TripStatus.COMPLETED and database.added == []


async def test_a_run_that_crashes_is_still_tidied_up_and_is_reported_under_its_trip(reporting):  # noqa: F811
    outbox = reporting()

    async def crashes(_agent, _db, _hooks) -> dict:
        raise RuntimeError("the graph fell over")

    with a_trip_being_planned() as (database, redis):
        task, trip_id = await started(crashes, redis, turn=3)
        await task  # a crash is handled inside the run: nothing is raised to whoever started it

    assert database.trip.status == TripStatus.FAILED
    assert redis.published[-1]["event"] == "planning_failed"
    (report,) = outbox.errors()  # logged as an error — which is how Sentry hears of a background run
    assert report["logentry"]["message"] == "Planning run crashed for trip %s"
    assert report["tags"] == {"trip_id": str(trip_id), "turn": "3"}
    assert report["exception"]["values"][-1]["value"] == "the graph fell over"


# ── A server that is stopping ──────────────────────────────────────────────


async def test_a_stopping_server_waits_for_the_plans_it_is_making(caplog):
    assert await finish_runs(5) == 0  # nothing in flight: nothing to wait for

    with a_trip_being_planned() as (database, redis), caplog.at_level(logging.INFO, logger="app.api.routes.trips"):

        async def a_quick_plan(_agent, _db, _hooks) -> dict:
            await asyncio.sleep(0.1)
            return {"itinerary_id": "saved"}

        task, _ = await started(a_quick_plan, redis)
        given_up = await finish_runs(grace_seconds=5)

    assert given_up == 0 and task.done() and not task.cancelled()  # it was let finish
    assert (
        database.trip.status == TripStatus.PLANNING and redis.published == []
    )  # nothing was marked: the run did its own
    assert "[SHUTDOWN] 1 planning run(s) in flight — waiting up to 5 s for them" in caplog.text


async def test_a_plan_that_does_not_finish_in_time_is_given_up_properly(caplog):
    with a_trip_being_planned() as (database, redis), caplog.at_level(logging.WARNING, logger="app.api.routes.trips"):
        task, _ = await started(never_finishes, redis)

        given_up = await finish_runs(grace_seconds=0.05)

    assert given_up == 1 and task.cancelled()
    assert database.trip.status == TripStatus.FAILED and redis.published[-1]["error"] == INTERRUPTED
    assert "[SHUTDOWN] 1 planning run(s) could not finish in time and were given up" in caplog.text


async def test_the_app_waits_for_its_runs_before_it_closes_what_they_need():
    order: list[str] = []
    step = lambda name: AsyncMock(side_effect=lambda *_a, **_k: order.append(name))  # noqa: E731

    with patch.multiple(
        main,
        _show_app_logs=MagicMock(),
        init_redis=AsyncMock(return_value=AsyncMock()),
        _recover_after_restart=AsyncMock(),
        finish_runs=step("the runs are waited for"),
        close_redis=step("redis is closed"),
        close_session=step("the tool server is closed"),
    ):
        async with main.lifespan(main.app):
            assert order == []

    assert order == ["the runs are waited for", "redis is closed", "the tool server is closed"]
    main.finish_runs  # noqa: B018 — imported by the app from routes/trips.py
    assert main.settings.SHUTDOWN_GRACE_SECONDS == 40.0


# ── An instance that is starting ───────────────────────────────────────────


async def test_a_starting_instance_can_tell_which_trips_another_one_is_still_planning(caplog):
    alive, dead = uuid.uuid4(), uuid.uuid4()
    redis = Redis()
    await save_current_run(redis, str(alive), {"turn": 1})

    with patch.object(main.redis_db, "redis_client", redis):
        assert await main._being_planned_elsewhere([alive, dead]) == {alive}
        assert await main._being_planned_elsewhere([]) == set()
    with patch.object(main.redis_db, "redis_client", None):  # Redis never came up: nobody can vouch for a run
        assert await main._being_planned_elsewhere([alive, dead]) == set()
    unreachable = MagicMock(exists=AsyncMock(side_effect=RedisConnectionError("refused")))
    with patch.object(main.redis_db, "redis_client", unreachable), caplog.at_level(logging.WARNING):
        # a trip "planning" for ever is worse than a run failed twice
        assert await main._being_planned_elsewhere([alive, dead]) == set()
    assert "Redis could not say which runs are alive" in caplog.text


# ── The page: its stream, and what it is told afterwards ───────────────────


class Subscription:
    """A pub/sub connection that plays a script: messages, quiet waits (None), and errors."""

    def __init__(self, script: list) -> None:
        self.script, self.subscribed, self.waits, self.closed = list(script), [], [], False

    async def subscribe(self, channel) -> None:
        self.subscribed.append(channel)

    async def get_message(self, ignore_subscribe_messages=False, timeout=None):
        self.waits.append((ignore_subscribe_messages, timeout))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def aclose(self) -> None:
        self.closed = True


@contextmanager
def the_app_with(trip, redis, rows=()):
    from app.api.deps import get_current_user, get_current_user_sse, get_db, get_redis_dep, get_redis_or_none
    from app.main import app

    db = AsyncMock()
    found, runs = MagicMock(), MagicMock()
    found.scalar_one_or_none.return_value = trip
    runs.scalars.return_value.all.return_value = list(rows)
    db.execute = AsyncMock(side_effect=[found, runs])

    async def database():
        yield db

    user = lambda: MagicMock(id=trip.user_id)  # noqa: E731
    app.dependency_overrides.update(
        {
            get_db: database,
            get_redis_dep: lambda: redis,
            get_redis_or_none: lambda: redis,
            get_current_user: user,
            get_current_user_sse: user,
        }
    )
    try:
        yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    finally:
        app.dependency_overrides.clear()


def events_of(body: str) -> list[tuple[str, dict]]:
    found, kind = [], None
    for line in body.splitlines():
        if line.startswith("event:"):
            kind = line[6:].strip()
        elif line.startswith("data:") and kind:
            found.append((kind, json.loads(line[5:].strip())))
    return found


async def test_the_event_stream_waits_in_pieces_and_ends_quietly_when_redis_cannot_be_reached_again(caplog):
    """One endless read used to be how it waited: the first connection a hosted Redis closed ended the stream
    with an error — or, closed without a word, with nothing at all, for ever."""
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit_event = None  # the library keeps one per event loop; each test has its own
    trip = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), status=TripStatus.PLANNING)
    progress = {"agent": "flight_agent", "status": "completed"}
    subscription = Subscription(
        [
            None,  # a quiet wait: nothing published yet
            {"type": "message", "data": json.dumps(progress)},
            None,
            {"type": "message", "data": json.dumps({"event": "planning_complete"})},
            RedisConnectionError("Connection closed by server."),  # and it could not be opened again
        ]
    )
    redis = MagicMock(pubsub=lambda: subscription)

    with the_app_with(trip, redis) as http, caplog.at_level(logging.WARNING, logger="app.api.routes.trips"):
        async with http:
            answer = await http.get(f"/trips/{trip.id}/stream")  # returns when the stream has ended

    assert answer.status_code == 200 and answer.headers["content-type"].startswith("text/event-stream")
    assert subscription.subscribed == [f"trip:{trip.id}:events"]
    assert events_of(answer.text) == [
        ("connected", {"trip_id": str(trip.id), "status": "listening", "trip_status": "planning"}),
        ("agent_update", progress),
        ("agent_update", {"event": "planning_complete"}),
    ]
    # each wait is bounded, and goes through the client — whose retry reconnects a subscription that was closed
    assert subscription.waits == [(True, trips.STREAM_WAIT_S)] * 5 and 0 < trips.STREAM_WAIT_S <= 15
    assert subscription.closed  # nothing is left open behind a stream that ended
    assert "Redis could not be reached again" in caplog.text  # ended, said once, and not as an unhandled error


async def test_a_page_loaded_after_an_interrupted_run_is_told_why_it_failed():
    trip = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), status=TripStatus.FAILED, destination="Goa", budget=50_000.0
    )
    rows = [
        AgentRun(trip_id=trip.id, agent_name="intent_parsing", status="completed", turn=1, input={}, output={}),
        AgentRun(trip_id=trip.id, agent_name="flight_agent", status="completed", turn=1, input={}, output={}),
        AgentRun(
            trip_id=trip.id, agent_name="interrupted", status="failed", turn=1, input={}, output={"reason": INTERRUPTED}
        ),
    ]

    with the_app_with(trip, Redis(), rows) as http:
        async with http:
            status = (await http.get(f"/trips/{trip.id}/status")).json()

    assert status["status"] == "failed" and status["failure_reason"] == INTERRUPTED


async def test_an_interrupted_run_is_over_and_what_it_did_is_not_counted_into_the_run_after_it():
    """A run the server stopped under writes no closing row. Its "interrupted" row is where it ends: the run the
    traveller starts next is not shown the dead one's searches, and if it fails, not the dead one's reason."""
    trip = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), status=TripStatus.FAILED, destination="Goa", budget=50_000.0
    )

    def row(agent: str, status: str = "completed", **output) -> AgentRun:
        return AgentRun(trip_id=trip.id, agent_name=agent, status=status, turn=1, input={}, output=output)

    first_run = [row("flight_agent"), row("hotel_agent"), row("interrupted", "failed", reason=INTERRUPTED)]
    its_own_failure = {"builder_error": {"error": "The plan named a place nobody had found."}}
    second_run = [
        row("flight_agent"),
        row("builder_failed", "failed", **its_own_failure),
        row("orchestrator", "failed"),
    ]

    with the_app_with(trip, Redis(), first_run + second_run) as http:
        async with http:
            status = (await http.get(f"/trips/{trip.id}/status")).json()
    assert status["failure_reason"] == "The plan named a place nobody had found."  # the second run's own reason

    trip.status = TripStatus.PLANNING  # the second run, while it is still going: one search done so far
    with the_app_with(trip, Redis(), first_run + [row("flight_agent")]) as http:
        async with http:
            in_flight = (await http.get(f"/trips/{trip.id}/status")).json()
    assert in_flight["progress"]["agents"] == {
        "flight_agent": "completed",
        "hotel_agent": "pending",  # the dead run's hotel search is not this run's
        "activities_agent": "pending",
    }

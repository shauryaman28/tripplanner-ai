"""Phase 32 — what the app does when its database goes away, and what it must not lose when it dies itself.

Postgres was killed while a plan was being made, and at the point between the two writes that save
one; the backend was killed between saving an itinerary and embedding it (docs/chaos-data-report.md).
Behind a pooler a short outage turned out to be a pause, not an error — and what was wrong was at
its edges: nothing bounded the wait, a traveller was shown the driver's own account of a failure
(the statement, its parameters), the pooler looked for its server again only every 15 seconds, and
an itinerary whose backend died before embedding it was never embedded.

These hold what was changed. The real Postgres — its connections killed under the pool, a save that
fails half-way — is in tests/integration/test_phase32_data_integration.py; the whole of it on a
running stack is scripts/chaos.py.
"""

import asyncio
import json
import re
import sys
import uuid
from importlib import util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import yaml
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.exc import InterfaceError as DriverInterfaceError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app import main
from app.api.routes import trips
from app.db import session
from app.db.errors import database_is_away
from app.models.embedding import Embedding
from app.models.trip import TripStatus
from loadtest.standins import faults, models
from src.ai.embeddings.embedder import PENDING_RETRY_MODEL
from src.ai.orchestrator import orchestrator

ROOT = Path(__file__).resolve().parents[2]


def script(name: str):
    spec = util.spec_from_file_location(f"scripts_{name}", ROOT / "scripts" / f"{name}.py")
    module = util.module_from_spec(spec)
    sys.modules[f"scripts_{name}"] = module
    spec.loader.exec_module(module)
    return module


chaos = script("chaos")

# ── "The database cannot be reached" ───────────────────────────────────────


def as_sqlalchemy_raises(driver_error: BaseException, statement: str = "SELECT users.id FROM users") -> DBAPIError:
    """A driver's error the way it reaches the app: wrapped, with the driver's own under it."""
    wrapped = RuntimeError(f"<class '{type(driver_error).__module__}.{type(driver_error).__name__}'>: {driver_error}")
    wrapped.__cause__ = driver_error
    return DBAPIError(statement, {}, wrapped)


# what the backend logged 31 times while Postgres was away: a plain DBAPIError, with this under it
CLOSED = asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation")


@pytest.mark.parametrize(
    "gone",
    [
        as_sqlalchemy_raises(CLOSED),
        as_sqlalchemy_raises(asyncpg.CannotConnectNowError("the database system is starting up")),
        as_sqlalchemy_raises(asyncpg.TooManyConnectionsError("sorry, too many clients already")),
        as_sqlalchemy_raises(asyncpg.InterfaceError("connection is closed")),
        as_sqlalchemy_raises(TimeoutError()),  # a statement that was not answered in the time it is given
        as_sqlalchemy_raises(ConnectionResetError("Connection reset by peer")),
        OperationalError("SELECT 1", {}, RuntimeError("server closed the connection unexpectedly")),
        DriverInterfaceError("SELECT 1", {}, RuntimeError("connection is closed")),
        PoolTimeout("QueuePool limit of size 10 overflow 30 reached, connection timed out"),
        ConnectionRefusedError(61, "Connection refused"),  # no pooler in the way: nothing to connect to
        CLOSED,
    ],
)
def test_a_database_that_is_not_there_is_known_for_what_it_is(gone):
    assert database_is_away(gone)


def test_a_connection_that_was_invalidated_under_a_statement_counts_too():
    dropped = DBAPIError("SELECT 1", {}, RuntimeError("?"), connection_invalidated=True)
    assert database_is_away(dropped)


@pytest.mark.parametrize(
    "refused",
    [
        IntegrityError("INSERT INTO users", {}, RuntimeError("duplicate key value violates unique constraint")),
        ProgrammingError("SELECT nope", {}, RuntimeError('column "nope" does not exist')),
        as_sqlalchemy_raises(asyncpg.UniqueViolationError("duplicate key")),
        as_sqlalchemy_raises(ValueError("invalid input for query argument")),
        ValueError("not a database error at all"),
        TimeoutError(),  # a deadline somewhere else — a planning run's — is not the database's doing
    ],
)
def test_a_statement_the_database_refused_is_not_the_database_being_away(refused):
    """It would be answered "try again shortly" — and fail again, for ever, with nobody told there is a bug."""
    assert not database_is_away(refused)


async def test_a_request_that_finds_the_database_away_is_told_to_come_back():
    answer = await main.database_is_away_handler(MagicMock(), as_sqlalchemy_raises(CLOSED))
    body = json.loads(answer.body)
    assert answer.status_code == 503 and answer.headers["retry-after"] == "5"
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE" and "try again shortly" in body["detail"]
    # what the driver said is the log's: nothing of it — the statement, the class — is in the answer
    assert not any(word in answer.body.decode() for word in ("asyncpg", "sqlalchemy", "SELECT", "users"))

    handlers = main.app.exception_handlers
    assert handlers[DBAPIError] is handlers[PoolTimeout] is handlers[ConnectionError] is main.database_is_away_handler


async def test_a_statement_the_database_refused_is_still_a_500_and_still_reported():
    a_bug = ProgrammingError("SELECT nope", {}, RuntimeError('column "nope" does not exist'))
    with pytest.raises(ProgrammingError):  # raised again: the 500 it always was, with its error report
        await main.database_is_away_handler(MagicMock(), a_bug)


def test_the_driver_is_not_given_a_timeout_of_its_own():
    """Tried, as the app's limit on how long a pooler may keep a query — and it made things worse: the driver's
    timeout only takes effect once the server answers its cancel, so requests that had been answered late became
    errors, all at the same instant, three seconds after Postgres was back."""
    made = {}

    def engine(url, **options):
        made.update(options)
        return MagicMock()

    with patch.object(session.sqlalchemy.ext.asyncio, "create_async_engine", engine):
        session.make_engine("postgresql://u:p@pooler:6432/db?sslmode=require")

    assert "command_timeout" not in made["connect_args"]
    assert made["pool_pre_ping"] is True  # a connection is still tried before it is used


# ── A run that ends because of it ──────────────────────────────────────────


def test_the_traveller_is_told_what_happened_never_what_was_raised():
    """With Postgres killed as a plan was saved the page showed the statement and its parameters — the plan itself."""
    lost = as_sqlalchemy_raises(CLOSED, "INSERT INTO itineraries (id, trip_id, structured_data) VALUES ($1, $2, $3)")
    assert trips._told(lost) == trips.DATABASE_AWAY
    assert trips._told(TimeoutError()) == trips.TOOK_TOO_LONG
    assert trips._told(RuntimeError("'NoneType' object has no attribute 'days'")) == trips.WENT_WRONG
    for said in (trips.DATABASE_AWAY, trips.TOOK_TOO_LONG, trips.WENT_WRONG):
        assert "start it again" in said
        assert not any(word in said for word in ("asyncpg", "sqlalchemy", "INSERT", "NoneType", "Traceback"))


def a_database(trip, *, away_for: int = 0, then: BaseException | None = None):
    """A session factory over one trip row. `away_for`: how many sessions fail first, as a database that is away does."""
    opened = {"n": 0, "added": [], "commits": 0}

    class Session:
        async def __aenter__(self):
            opened["n"] += 1
            if opened["n"] <= away_for:
                raise then or as_sqlalchemy_raises(CLOSED)
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, _model, _key):
            return trip

        def add(self, row) -> None:
            opened["added"].append(row)

        async def commit(self) -> None:
            opened["commits"] += 1

    return Session, opened


async def test_a_run_that_could_not_mark_its_trip_goes_on_trying_until_the_database_is_back():
    trip = MagicMock(status=TripStatus.PLANNING)
    factory, seen = a_database(trip, away_for=3)
    with patch.object(trips, "AsyncSessionLocal", factory), patch.object(trips, "SETTLE_EVERY_S", 0):
        await trips._settle(uuid.uuid4(), 1, TripStatus.FAILED, trips.DATABASE_AWAY)

    assert seen["n"] == 4 and trip.status == TripStatus.FAILED and seen["commits"] == 1
    (row,) = seen["added"]  # the run's closing row: where GET /status reads the reason for a page loaded later
    assert (row.agent_name, row.status, row.output) == ("interrupted", "failed", {"reason": trips.DATABASE_AWAY})


async def test_it_gives_up_in_the_end_and_on_anything_that_is_not_the_database_being_away(caplog):
    trip = MagicMock(status=TripStatus.PLANNING)
    never, seen = a_database(trip, away_for=10**6)
    with (
        patch.object(trips, "AsyncSessionLocal", never),
        patch.object(trips, "SETTLE_EVERY_S", 0),
        patch.object(trips, "SETTLE_FOR_S", 0.05),
        caplog.at_level("ERROR"),
    ):
        await asyncio.wait_for(trips._settle(uuid.uuid4(), 1, TripStatus.FAILED, "why"), 2)  # returns: never raises
    assert trip.status == TripStatus.PLANNING and seen["n"] >= 2  # start-up recovery is what is left for it

    a_bug, seen = a_database(
        trip, away_for=1, then=ProgrammingError("UPDATE trips", {}, RuntimeError("no such column"))
    )
    with patch.object(trips, "AsyncSessionLocal", a_bug), patch.object(trips, "SETTLE_EVERY_S", 0):
        await trips._settle(uuid.uuid4(), 1, TripStatus.FAILED, "why")
    assert seen["n"] == 1  # not tried again: it would fail the same way for ten minutes
    assert trips.SETTLE_FOR_S == 600.0 and 1 <= trips.SETTLE_EVERY_S <= 5


async def test_a_mark_that_comes_late_does_not_undo_what_the_traveller_has_started_since():
    replanned = MagicMock(status=TripStatus.COMPLETED)  # asked for again and made, while the first run was still trying
    factory, seen = a_database(replanned)
    with patch.object(trips, "AsyncSessionLocal", factory):
        await trips._settle(uuid.uuid4(), 1, TripStatus.FAILED, trips.DATABASE_AWAY)
    assert replanned.status == TripStatus.COMPLETED and seen["commits"] == 0 and not seen["added"]

    # a refinement that made no plan: the graph has marked the trip failed, and the plan it had still stands
    refined = MagicMock(status=TripStatus.FAILED)
    factory, seen = a_database(refined)
    with patch.object(trips, "AsyncSessionLocal", factory):
        await trips._settle(uuid.uuid4(), 2, TripStatus.COMPLETED, None)
    assert (
        refined.status == TripStatus.COMPLETED and not seen["added"]
    )  # and no row: nothing failed that needs a reason


async def test_a_run_that_lost_its_database_tells_the_page_so_and_marks_its_trip_when_it_can():
    trip = MagicMock(status=TripStatus.PLANNING)
    factory, seen = a_database(trip)
    redis = AsyncMock()
    lost = as_sqlalchemy_raises(CLOSED, "INSERT INTO itineraries (id, trip_id, structured_data) VALUES ($1, $2, $3)")

    with patch.object(trips, "AsyncSessionLocal", factory):
        await trips._run_orchestrator(uuid.uuid4(), redis, AsyncMock(side_effect=lost))

    event = json.loads(redis.publish.await_args.args[1])
    assert (event["event"], event["error"]) == ("planning_failed", trips.DATABASE_AWAY)
    assert "INSERT" not in json.dumps(event) and "asyncpg" not in json.dumps(event)
    assert trip.status == TripStatus.FAILED and seen["added"][0].output == {"reason": trips.DATABASE_AWAY}


def test_a_run_that_ended_that_way_is_not_said_to_be_a_restart():
    assert trips._AGENT_LABELS["interrupted"] == "Stopped before it finished"


# ── An itinerary is saved owing its embeddings ─────────────────────────────


async def test_the_row_that_says_an_embedding_is_owed_is_in_the_commit_that_saves_the_itinerary():
    """Killed between saving a plan and embedding it, a backend left an itinerary with no row at all in
    `embeddings` — and start-up recovery looks for rows that say "owed"."""
    written: list = []
    db = MagicMock()
    db.get = AsyncMock(return_value=MagicMock(status=TripStatus.PLANNING))
    db.add = lambda row: written.append(("add", row))
    db.flush = AsyncMock(side_effect=lambda: written.append(("flush", None)))
    db.commit = AsyncMock(side_effect=lambda: written.append(("commit", None)))
    db.refresh = AsyncMock()
    state = {"db": db, "trip_id": uuid.uuid4(), "draft_itinerary": {"days": [], "total_cost": 0}}

    def not_now(job) -> None:
        job.close()

    with (
        patch.object(orchestrator, "_log", AsyncMock()),
        patch.object(orchestrator, "spawn", not_now) as _,
        patch.object(orchestrator.metrics, "itinerary_build"),
    ):
        saved = await orchestrator.persist_node(state)

    what = [kind for kind, _ in written]
    assert what.count("commit") == 1 and what[-1] == "commit"  # one transaction: everything, or nothing
    # the itinerary is sent before the row that points at it: no relationship tells the ORM their order, and
    # written in one flush the embedding went first — a foreign-key error that failed every plan
    sent = [type(row).__name__ if row is not None else kind for kind, row in written]
    assert sent.index("Itinerary") < sent.index("flush") < sent.index("Embedding")
    rows = [row for kind, row in written if kind == "add"]
    (owed,) = [row for row in rows if isinstance(row, Embedding)]
    (itinerary,) = [row for row in rows if type(row).__name__ == "Itinerary"]
    assert owed.itinerary_id == itinerary.id == saved["itinerary_id"]
    assert owed.embedding_model == PENDING_RETRY_MODEL and owed.vector is None


async def test_a_backend_that_starts_while_its_database_is_away_looks_again():
    """ "Skipped" used to be the end of it: what should have been recovered waited for the next restart."""
    later: list = []

    def spawned(job) -> None:
        later.append(job)
        job.close()

    class Away:
        async def __aenter__(self):
            raise as_sqlalchemy_raises(CLOSED)

        async def __aexit__(self, *_exc):
            return False

    with patch.object(main, "AsyncSessionLocal", Away), patch.object(main, "spawn", spawned):
        await main._recover_after_restart()
        assert len(later) == 1 and later[0].__name__ == "_recover_later"
        await main._recover_after_restart(again=0)  # …but not for ever
        assert len(later) == 1
    assert main.RECOVER_EVERY_S * main.RECOVER_TIMES == 600

    class Broken:
        async def __aenter__(self):
            raise ProgrammingError("SELECT", {}, RuntimeError('relation "trips" does not exist'))

        async def __aexit__(self, *_exc):
            return False

    with patch.object(main, "AsyncSessionLocal", Broken), patch.object(main, "spawn", spawned):
        await main._recover_after_restart()  # a database nobody has migrated is not one that is "away"
    assert len(later) == 1


# ── The stack, the faults, the script ──────────────────────────────────────


def test_the_pooler_looks_for_its_database_again_within_the_roadmaps_ten_seconds():
    pooler = (ROOT / "docker/prodlike/pgbouncer.ini").read_text()
    (retry,) = re.findall(r"^server_login_retry = (\d+)$", pooler, flags=re.M)
    assert int(retry) <= chaos.APP_MUST_ANSWER_WITHIN_S / 2 and chaos.APP_MUST_ANSWER_WITHIN_S == 10


def test_for_chaos_postgres_keeps_its_data_when_it_is_killed():
    """The production-like stack's database is in memory: killed and started, it would come back empty —
    and an empty database says nothing about a failover."""
    overlay = yaml.safe_load((ROOT / "docker/chaos/compose.yml").read_text().replace("!reset []", "[]"))
    database = overlay["services"]["neon-standin"]
    assert database == {"tmpfs": [], "volumes": ["chaos-pgdata:/var/lib/postgresql/data"]}
    assert "tmpfs: !reset []" in (ROOT / "docker/chaos/compose.yml").read_text()  # taken away, not added to
    assert "chaos-pgdata" in overlay["volumes"]
    assert "/var/lib/postgresql/data" in (ROOT / "docker/prodlike/compose.yml").read_text()  # the tmpfs it replaces


async def test_the_embedding_api_can_be_made_to_fail_and_only_while_a_fault_says_so(tmp_path):
    file = tmp_path / "standin-faults.json"
    with patch.object(faults, "FILE", file), patch.object(models.timing, "scale", 0):
        assert len(await models.embed("forts and food in Jaipur")) > 100  # no fault: it answers
        file.write_text(json.dumps({"embedding": "500"}))
        with pytest.raises(faults.EmbeddingFailed, match="500"):
            await models.embed("forts and food in Jaipur")
        file.unlink()
        assert await models.embed("forts and food in Jaipur")
    assert "embedding" in faults.WHO


def test_the_exercises_ask_for_a_travellers_words_and_know_the_drivers():
    shown = (
        "(sqlalchemy.dialects.postgresql.asyncpg.Error) <class 'asyncpg.exceptions.ConnectionDoesNotExistError'>: "
        "connection was closed in the middle of operation [SQL: INSERT INTO itineraries …]"
    )
    assert not chaos._in_a_travellers_words(shown) and not chaos._in_a_travellers_words(None)
    assert chaos._in_a_travellers_words(trips.DATABASE_AWAY) and chaos._in_a_travellers_words(trips.WENT_WRONG)


def test_a_plan_whose_backend_died_after_saving_it_is_not_counted_as_half_done():
    """The steps after the save died with the process; the plan stands. (Found by this count, and left as it is.)"""
    closes = chaos.INTEGRITY["trips whose rows do not end with the row that closes a run"]
    assert "'persist'" in closes and "'orchestrator'" in closes and "'interrupted'" in closes


def test_the_mid_save_exercise_stops_the_second_write_not_the_first():
    """Its first version held a lock that stopped the insert — and killed Postgres with nothing written."""
    source = (ROOT / "scripts/chaos.py").read_text()
    assert "FOR NO KEY UPDATE" in source and "LOCK TABLE itineraries" not in source
    assert "a.query ilike 'update trips%'" in chaos._WAITING and "c.relname = 'itineraries'" in chaos._WAITING

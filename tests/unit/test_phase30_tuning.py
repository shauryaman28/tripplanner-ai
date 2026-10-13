"""Phase 30 — what the load test changed, and what holds it in place.

The baseline found the app waiting for database connections (docs/load-test-report.md). Three things
were changed, one after the other, each with its hypothesis written first and the same load run
again afterwards:

- **The connections are a setting, and the stack's numbers fit together.** The app's pool was the
  library's default and the stand-in pooler lent four connections; now `DB_POOL_SIZE` and
  `DB_MAX_OVERFLOW` say what the pool is, and the production-like stack's pooler lends what the
  hosted one does.
- **No transaction is left open while something outside is waited for.** That was the cause: a
  planning run read a row back after committing it, and kept the connection — idle, inside the
  transaction that read began — until its next step, a model call later. More connections moved the
  wall; this takes it away.
- **The Redis pool is a setting.** The library opens a hundred connections and no more, every open
  trip page keeps one, and the hundred-and-first page failed.

These tests hold the numbers to each other — a pool larger than what the pooler can give it is a
queue moved, not removed — and hold every place that reads and then waits to giving the connection
back in between. The integration tests ask the same of a whole plan on a real database
(tests/integration/test_phase30_connections_integration.py).
"""

import inspect
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from pydantic import ValidationError
from sqlalchemy.pool import QueuePool

from app.core.config import Settings, settings
from app.db import redis as redis_module
from app.db import session as db_session_module
from app.models.itinerary import Itinerary
from app.models.trip import Trip, TripStatus
from src.ai.agents import preference_extractor
from src.ai.agents.refinement_classifier import RefinementClassification
from src.ai.embeddings import embedder
from src.ai.orchestrator import orchestrator
from src.ai.usage import Usage
from src.ai.utils import embeddings as embedding_job
from src.ai.utils import run_logger
from src.ai.utils.run_logger import hand_back, log_agent_run

ROOT = Path(__file__).resolve().parents[2]
PRODLIKE = yaml.safe_load((ROOT / "docker/prodlike/compose.yml").read_text())
POOLER = (ROOT / "docker/prodlike/pgbouncer.ini").read_text()
NEON = "postgresql://user:pw@ep-cool-name-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"


# ── The database pool is a setting ─────────────────────────────────────────


def test_the_engine_is_made_with_the_pool_the_settings_say():
    with (
        patch.object(settings, "DB_POOL_SIZE", 10),
        patch.object(settings, "DB_MAX_OVERFLOW", 30),
        patch("sqlalchemy.ext.asyncio.create_async_engine") as create,
    ):
        db_session_module.make_engine(NEON)

    made_with = create.call_args.kwargs
    assert (made_with["pool_size"], made_with["max_overflow"]) == (10, 30)
    assert made_with["pool_pre_ping"] is True and made_with["pool_recycle"] == 300  # what Phase 27 set, still set


def test_someone_who_sets_nothing_gets_the_pool_the_app_always_had():
    """The defaults are the library's own: the setting names what was there, and changes nothing by existing."""
    library = inspect.signature(QueuePool.__init__).parameters

    assert Settings.model_fields["DB_POOL_SIZE"].default == library["pool_size"].default == 5
    assert Settings.model_fields["DB_MAX_OVERFLOW"].default == library["max_overflow"].default == 10


@pytest.mark.parametrize(("name", "value"), [("DB_POOL_SIZE", 0), ("DB_POOL_SIZE", -1), ("DB_MAX_OVERFLOW", -1)])
def test_a_pool_of_nothing_is_refused_as_the_settings_are_read(name, value):
    """`max_overflow=-1` is the library's word for "no limit at all": not something to arrive at by a typo."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


def test_what_get_metrics_calls_the_limit_is_what_the_settings_add_up_to():
    pool = db_session_module.engine.pool

    assert (pool.size(), pool._max_overflow) == (settings.DB_POOL_SIZE, settings.DB_MAX_OVERFLOW)
    assert REGISTRY.get_sample_value("db_pool_connections_limit") == settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW


# ── The production-like stack's numbers fit together ───────────────────────


def lent_by_the_pooler() -> int:
    return int(re.search(r"^default_pool_size = (\d+)$", POOLER, flags=re.M).group(1))


def held_by_one_instance() -> int:
    given = PRODLIKE["services"]["backend"]["environment"]
    return int(given["DB_POOL_SIZE"]) + int(given["DB_MAX_OVERFLOW"])


def test_two_instances_fit_in_what_the_pooler_lends():
    """A deploy starts the new instance while the old one is still serving: for a while there are two pools."""
    assert held_by_one_instance() == 40 and lent_by_the_pooler() == 90
    assert 2 * held_by_one_instance() <= lent_by_the_pooler()
    # the overlap is real: the platform is told to keep the old instance for this long
    assert int(__import__("json").loads((ROOT / "railway.json").read_text())["deploy"]["overlapSeconds"]) > 0


def test_the_pooler_lends_no_more_than_the_database_has():
    """Postgres here allows 100 connections (its default: the stack sets none); the migrations and a health
    check go around the pooler, and need some of them."""
    postgres = PRODLIKE["services"]["neon-standin"]
    assert "max_connections" not in postgres["command"]  # so it is the default, 100
    assert lent_by_the_pooler() <= 100 - 5
    assert int(re.search(r"^max_client_conn = (\d+)$", POOLER, flags=re.M).group(1)) >= 2 * held_by_one_instance()
    assert "pool_mode = transaction" in POOLER  # and it is still the kind of pooler a hosted Postgres hands out


def test_a_deployment_is_told_about_the_two_settings():
    example = (ROOT / ".env.example").read_text()
    assert "# DB_POOL_SIZE=10" in example and "# DB_MAX_OVERFLOW=30" in example
    how_to = (ROOT / "HOW_TO_RUN.md").read_text()
    assert "`DB_POOL_SIZE`" in how_to and "`DB_MAX_OVERFLOW`" in how_to


# ── No transaction is left open while something outside is waited for ──────


class Session:
    """A database session as far as these tests need one: it knows whether a transaction is open.

    As the real one does: the first statement begins a transaction and takes a connection, and only a
    commit or a rollback ends it and gives the connection back.
    """

    def __init__(self, **found) -> None:
        self.open, self.did, self.added, self._found = False, [], [], found

    def _began(self, what: str) -> None:
        self.open = True
        self.did.append(what)

    def add(self, row) -> None:
        self.added.append(row)
        self.did.append("add")

    async def get(self, model, _key):
        self._began(f"get {model.__name__}")
        return self._found.get(model.__name__)

    async def scalar(self, _statement):
        self._began("scalar")
        return 0

    async def execute(self, _statement):
        self._began("execute")
        nothing = MagicMock()
        nothing.scalars.return_value.all.return_value = []
        return nothing

    async def refresh(self, _row) -> None:
        self._began("refresh")

    async def delete(self, _row) -> None:
        self.did.append("delete")

    async def commit(self) -> None:
        self.open = False
        self.did.append("commit")

    async def rollback(self) -> None:
        self.open = False
        self.did.append("rollback")


def asked_a_model() -> Usage:
    usage = Usage("itinerary_builder")
    usage.add("openai/gpt-oss-120b", 2_400, 1_900)
    return usage


async def test_a_logged_step_ends_its_transaction_and_reads_nothing_back():
    """The cause of the baseline's wall: the row was read back, and that read's transaction stayed open
    until the next step — a model call later."""
    db = Session()

    row = await log_agent_run(db, uuid.uuid4(), "flight_agent", {"origin": "DEL"}, {"flights": []}, 14)

    assert db.did == ["add", "commit"] and db.open is False
    assert row.agent_name == "flight_agent" and row.id and row.created_at  # every column is set here: nothing to read


async def test_a_step_that_asked_a_model_adds_up_the_trips_cost_and_ends_that_transaction_too():
    db = Session()

    row = await log_agent_run(db, uuid.uuid4(), "itinerary_builder", {}, {}, 5_300, usage=asked_a_model())

    assert row.cost_usd and db.did == ["add", "commit", "scalar", "scalar", "commit"]  # the two sums, then given back
    assert db.open is False


async def test_handing_back_is_a_commit_and_never_an_error(caplog):
    db = Session()
    await db.get(Trip, uuid.uuid4())
    assert db.open is True

    await hand_back(db)
    assert db.open is False and db.did[-1] == "commit"

    gone = AsyncMock()
    gone.commit.side_effect = ConnectionError("the connection was closed")
    with caplog.at_level(logging.WARNING, logger=run_logger.__name__):
        await hand_back(gone)  # what the caller does next is what matters
    assert "A read's transaction could not be ended" in caplog.text


async def test_the_preference_extractor_asks_its_model_with_no_connection_in_hand():
    """After planning_complete: who the trip is for is looked up — and then a model is asked, for a second."""
    db = Session(Trip=MagicMock(user_id=uuid.uuid4()))
    held: list[bool] = []

    async def the_model(_system: str, _prompt: str) -> str:
        held.append(db.open)
        return '{"travel_style": "mid-range", "dietary_restrictions": [], "preferred_airlines": [], "home_city": null}'

    state = {"db": db, "trip_id": uuid.uuid4(), "draft_itinerary": {"days": [], "total_cost": 0}, "turn": 1}
    with (
        patch.object(preference_extractor, "_call_llm", the_model),
        patch.object(preference_extractor.settings, "GROQ_API_KEY", "a-key"),
    ):
        await orchestrator.extract_preferences_node(state)

    assert held == [False]
    assert db.did[:2] == ["get Trip", "commit"] and db.open is False  # looked up, handed back — then the model


async def test_looking_up_whose_trip_a_trace_is_for_does_not_keep_the_connection():
    owner = uuid.uuid4()
    db = Session(Trip=MagicMock(user_id=owner))

    assert await orchestrator._user_of({"db": db, "trip_id": uuid.uuid4()}) == owner
    assert db.did == ["get Trip", "commit"] and db.open is False


@asynccontextmanager
async def a_session_of(db: Session):
    yield db


async def test_the_embedding_job_asks_the_api_before_it_touches_the_database_again():
    """It kept a connection for both calls to the embedding API — 4 seconds of one real plan's, and far longer
    when the API answers 429 and is asked again."""
    itinerary_id = uuid.uuid4()
    itinerary = Itinerary(id=itinerary_id, trip_id=uuid.uuid4(), structured_data={"days": []}, total_cost=0)
    db = Session(Itinerary=itinerary, Trip=MagicMock(destination="Goa", interests=["beach"]))
    while_asked: list[tuple[bool, int]] = []

    async def the_api(_text: str, _task: str = "") -> list[float]:
        while_asked.append((db.open, len(db.did)))
        return [0.1] * 1536

    with (
        patch.object(db_session_module, "AsyncSessionLocal", lambda: a_session_of(db)),
        patch.object(embedder, "_call_embed", the_api),
        patch.object(redis_module, "redis_client", None),
    ):
        await embedding_job.generate_embeddings(itinerary_id)

    assert [held for held, _ in while_asked] == [False, False]  # two texts, two calls, no connection held for either
    assert db.did == ["get Itinerary", "get Trip", "commit", "execute", "add", "add", "commit"]
    assert {done for _, done in while_asked} == {
        3
    }  # both calls came after the reads were handed back, before the writes
    assert [row.kind for row in db.added] == [embedder.FULL_TEXT, embedder.SUMMARY] and db.open is False


@contextmanager
def the_api_with(db, redis=None):
    """The app with its database session, its Redis and its user stood in for."""
    from app.api.deps import get_current_user, get_db, get_redis_dep, get_redis_or_none
    from app.main import app

    async def _db():
        yield db

    app.dependency_overrides.update(
        {
            get_db: _db,
            get_redis_dep: lambda: redis,
            get_redis_or_none: lambda: redis,
            get_current_user: lambda: MagicMock(id=uuid.uuid4()),
        }
    )
    try:
        yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    finally:
        app.dependency_overrides.clear()


def last_thing_done_with(db) -> str:
    return db.mock_calls[-1][0] if db.mock_calls else "nothing"


async def test_a_search_embeds_what_was_typed_with_no_connection_in_hand():
    db = AsyncMock()
    before_the_model: list[str] = []

    async def embed(_query: str) -> list[float]:
        before_the_model.append(last_thing_done_with(db))
        return [0.1] * 1536

    with (
        the_api_with(db) as client,
        patch("app.api.routes.trips.embed_query", embed),
        patch("app.api.routes.trips.search_trips", AsyncMock(return_value=[])),
    ):
        answer = await client.get("/trips/search", params={"q": "quiet beaches"})

    assert answer.status_code == 200 and before_the_model == ["commit"]


async def test_a_change_request_is_read_by_a_model_with_no_connection_in_hand():
    """POST /refine looks the trip up, and then a model reads the message — a second or two, on the request's path."""
    trip_id, owner = uuid.uuid4(), uuid.uuid4()
    trip = Trip(
        id=trip_id,
        user_id=owner,
        destination="Goa",
        start_date="2030-01-10",
        end_date="2030-01-13",
        budget=50_000,
        status=TripStatus.COMPLETED,
    )
    found = MagicMock()
    found.scalar_one_or_none.return_value = trip
    db = AsyncMock()
    db.execute = AsyncMock(return_value=found)
    db.add = MagicMock()
    redis = AsyncMock()
    kept = {"planning_state": {"destination": "Goa", "flights": []}, "conv_history": []}
    redis.get = AsyncMock(side_effect=lambda key: json.dumps(kept[key.rsplit(":", 1)[-1]]))
    before_the_model: list[str] = []

    async def classify(_message: str, _history) -> RefinementClassification:
        before_the_model.append(last_thing_done_with(db))
        return RefinementClassification(refinement_type="targeted_hotel", reason="a different hotel")

    with (
        the_api_with(db, redis) as client,
        patch("app.api.routes.trips.classify_refinement", classify),
        patch("app.api.routes.trips.get_history", AsyncMock(return_value=[])),
        patch("app.api.routes.trips.get_current_turn", AsyncMock(return_value=1)),
        patch("app.api.routes.trips._start_run", AsyncMock()) as started,
    ):
        answer = await client.post(f"/trips/{trip_id}/refine", json={"message": "a nicer hotel, please"})

    assert answer.status_code == 200 and answer.json()["refinement_type"] == "targeted_hotel"
    assert before_the_model == ["commit"]  # the lookup's transaction was ended before the model was asked
    started.assert_awaited_once()  # …and the trip row it had read is still what the run is started on


# ── The Redis pool is a setting ────────────────────────────────────────────


async def test_the_redis_client_is_made_with_as_many_connections_as_the_settings_say():
    with (
        patch.object(settings, "REDIS_MAX_CONNECTIONS", 500),
        patch.object(redis_module.aioredis, "from_url") as made,
        patch.object(redis_module.traces, "instrument_redis"),
    ):
        was = redis_module.redis_client
        try:
            await redis_module.init_redis()
        finally:
            redis_module.redis_client = was

    assert made.call_args.kwargs["max_connections"] == 500
    assert made.call_args.kwargs["health_check_interval"] == redis_module.HEALTH_CHECK_S  # and as Phase 27 made it


def test_someone_who_sets_nothing_gets_the_hundred_connections_the_app_always_had():
    import redis.asyncio as aioredis

    library = aioredis.ConnectionPool.from_url("redis://localhost:6379").max_connections

    assert Settings.model_fields["REDIS_MAX_CONNECTIONS"].default == library == 100
    with pytest.raises(ValidationError):
        Settings(_env_file=None, REDIS_MAX_CONNECTIONS=0)


def test_the_production_like_stack_can_have_five_hundred_pages_open():
    given = PRODLIKE["services"]["backend"]["environment"]

    assert given["REDIS_MAX_CONNECTIONS"] == "500"
    # the Redis it talks to takes more than that: its own limit is the server's default of 10,000 clients
    assert "maxclients" not in PRODLIKE["services"]["upstash-standin"]["command"]
    example = (ROOT / ".env.example").read_text()
    assert "# REDIS_MAX_CONNECTIONS=500" in example
    assert "`REDIS_MAX_CONNECTIONS`" in (ROOT / "HOW_TO_RUN.md").read_text()

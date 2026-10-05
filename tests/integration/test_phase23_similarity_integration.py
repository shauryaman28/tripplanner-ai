"""Phase 23 — similar trips and search, on real Postgres + pgvector.

    RUN_INTEGRATION=1 pytest tests/integration/test_phase23_similarity_integration.py -v

Most tests place trips at an exact similarity to the query: every vector lies in
one plane, so the cosine between two of them is the cosine of the angle between
them. The last tests plan trips through the whole pipeline instead, on the
stand-in embedder (tests/fakes.py).
"""

import math
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.dialects import postgresql
from sqlmodel import select

from app import search
from app.core.config import settings
from app.models.embedding import Embedding
from app.models.itinerary import Itinerary
from app.models.trip import Trip, TripStatus
from app.models.user import User
from app.search import Match, nearest_trips, search_trips, similar_trips, summary_vector
from src.ai.embeddings.embedder import FULL_TEXT, PENDING_RETRY_MODEL, SUMMARY
from tests.fakes import ATTRACTIONS, MOUNTAIN_ATTRACTIONS, flight
from tests.integration.test_pipeline_integration import _login, _run_finished, _stack, _until

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

START = date.today() + timedelta(days=30)


def at(similarity: float) -> list[float]:
    """A vector whose cosine similarity to QUERY is exactly `similarity`."""
    angle = math.acos(similarity)
    return [math.cos(angle), math.sin(angle)] + [0.0] * 1534


QUERY = at(1.0)
ELSEWHERE = [0.0, 0.0, 1.0] + [0.0] * 1533  # at right angles to all of the above: similarity 0


def _plan(place: str, rating: float = 2.0) -> dict:
    return {
        "days": [{"day": 1, "date": str(START), "morning": {"activity": place, "rating": rating}}],
        "total_cost": 9_000,
    }


async def _user(db) -> User:
    user = User(email=f"{uuid.uuid4()}@example.com", hashed_password="x")
    db.add(user)
    await db.commit()
    return user


async def _trip(
    db,
    user: User,
    destination: str,
    similarity: float | None,
    *,
    status: str = TripStatus.COMPLETED,
    full_text: list[float] | None = None,
    earlier: float | None = None,
) -> Trip:
    """A planned trip whose latest itinerary's summary is at `similarity` to QUERY (None: no summary yet).

    `earlier`: an older itinerary of the same trip, with a summary at that similarity.
    `full_text`: the vector of the itinerary's other embedding (at right angles to QUERY by default).
    """
    trip = Trip(
        user_id=user.id,
        destination=destination,
        start_date=START,
        end_date=START + timedelta(days=2),
        budget=20_000,
        status=status,
    )
    db.add(trip)
    await db.commit()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    versions = [(earlier, now - timedelta(hours=1))] if earlier is not None else []
    for summary, created in [*versions, (similarity, now)]:
        itinerary = Itinerary(
            trip_id=trip.id, structured_data=_plan(f"{destination} Fort"), total_cost=9_000.0, created_at=created
        )
        db.add(itinerary)
        await db.commit()
        if summary is None:
            db.add(Embedding(itinerary_id=itinerary.id, embedding_model=PENDING_RETRY_MODEL))
        else:
            db.add(Embedding(itinerary_id=itinerary.id, embedding_model="test", kind=SUMMARY, vector=at(summary)))
            db.add(
                Embedding(
                    itinerary_id=itinerary.id, embedding_model="test", kind=FULL_TEXT, vector=full_text or ELSEWHERE
                )
            )
        await db.commit()
    return trip


def _found(matches: list[Match]) -> list[tuple[str, float]]:
    return [(match.trip.destination, round(match.similarity, 3)) for match in matches]


# ── Ranking ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trips_come_back_nearest_first_with_their_cosine_similarity(db_session):
    me = await _user(db_session)
    for destination, similarity in (("Hampi", 0.62), ("Goa", 0.95), ("Leh", 0.31), ("Varkala", 0.88)):
        await _trip(db_session, me, destination, similarity)

    assert _found(await nearest_trips(db_session, QUERY, me.id, limit=10)) == [
        ("Goa", 0.95),
        ("Varkala", 0.88),
        ("Hampi", 0.62),
        ("Leh", 0.31),
    ]
    assert _found(await nearest_trips(db_session, QUERY, me.id, limit=2)) == [("Goa", 0.95), ("Varkala", 0.88)]
    assert _found(await nearest_trips(db_session, QUERY, me.id, limit=10, floor=0.6)) == [
        ("Goa", 0.95),
        ("Varkala", 0.88),
        ("Hampi", 0.62),
    ]


@pytest.mark.asyncio
async def test_only_the_travellers_own_trips_are_ever_compared(db_session):
    """An itinerary says where someone is going and when: nobody else's is shown, however alike."""
    me, someone_else = await _user(db_session), await _user(db_session)
    await _trip(db_session, me, "Hampi", 0.70)
    await _trip(db_session, someone_else, "Their Goa", 0.99)

    assert _found(await nearest_trips(db_session, QUERY, me.id, limit=10)) == [("Hampi", 0.7)]
    assert _found(await nearest_trips(db_session, QUERY, someone_else.id, limit=10)) == [("Their Goa", 0.99)]


@pytest.mark.asyncio
async def test_a_trip_is_compared_by_its_latest_itinerary_and_listed_once(db_session):
    me = await _user(db_session)
    await _trip(db_session, me, "Was a beach trip", 0.20, earlier=0.97)  # refined into something else
    await _trip(db_session, me, "Became a beach trip", 0.90, earlier=0.10)

    assert _found(await nearest_trips(db_session, QUERY, me.id, limit=10)) == [
        ("Became a beach trip", 0.9),
        ("Was a beach trip", 0.2),
    ]


@pytest.mark.asyncio
async def test_only_summaries_are_compared_and_only_plans_that_stand(db_session):
    me = await _user(db_session)
    await _trip(db_session, me, "Summary far, full text near", 0.30, full_text=at(0.99))
    await _trip(db_session, me, "Being changed", 0.80, status=TripStatus.PLANNING)  # its plan is still there
    await _trip(db_session, me, "Re-plan failed", 0.95, status=TripStatus.FAILED)  # the page shows the failure
    await _trip(db_session, me, "Not embedded yet", None)

    assert _found(await nearest_trips(db_session, QUERY, me.id, limit=10)) == [
        ("Being changed", 0.8),
        ("Summary far, full text near", 0.3),
    ]


# ── Similar trips ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_similar_trips_are_the_five_most_alike_above_the_floor_and_never_the_trip_itself(db_session):
    """Roadmap acceptance: up to five results, with similarity scores."""
    me = await _user(db_session)
    goa = await _trip(db_session, me, "Goa", 1.0)
    for n, similarity in enumerate((0.96, 0.93, 0.91, 0.89, 0.87, 0.85), start=1):
        await _trip(db_session, me, f"Beach {n}", similarity)
    await _trip(db_session, me, "Leh", 0.83)  # just under the floor: the nearest of the rest is not "similar"

    assert await summary_vector(db_session, goa.id) == pytest.approx(QUERY)
    matches = await similar_trips(db_session, goa)
    assert _found(matches) == [
        ("Beach 1", 0.96),
        ("Beach 2", 0.93),
        ("Beach 3", 0.91),
        ("Beach 4", 0.89),
        ("Beach 5", 0.87),
    ]
    assert _found(await similar_trips(db_session, goa, limit=3)) == _found(matches)[:3]
    assert matches[0].highlight == "Beach 1 Fort"


@pytest.mark.asyncio
async def test_a_trip_with_nothing_like_it_has_no_similar_trips(db_session):
    me = await _user(db_session)
    goa = await _trip(db_session, me, "Goa", 1.0)
    await _trip(db_session, me, "Leh", search.SIMILAR_FLOOR - 0.01)
    assert await similar_trips(db_session, goa) == []


@pytest.mark.asyncio
async def test_a_trip_that_is_not_embedded_yet_cannot_be_compared(db_session):
    me = await _user(db_session)
    fresh = await _trip(db_session, me, "Just planned", None)
    await _trip(db_session, me, "Goa", 0.9)
    assert await summary_vector(db_session, fresh.id) is None
    assert await similar_trips(db_session, fresh) is None  # "pending", not "nothing alike"


# ── Search ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_returns_the_trips_close_to_the_best_match_and_nothing_when_the_best_is_far(db_session):
    me = await _user(db_session)
    for destination, similarity in (("Goa", 0.70), ("Varkala", 0.68), ("Hampi", 0.65), ("Leh", 0.59)):
        await _trip(db_session, me, destination, similarity)

    # within 0.04 of the best match — Hampi and Leh are above the floor but not close to it
    assert _found(await search_trips(db_session, me.id, QUERY)) == [("Goa", 0.7), ("Varkala", 0.68)]

    nobody = await _user(db_session)
    await _trip(db_session, nobody, "Leh", search.SEARCH_FLOOR - 0.01)
    assert await search_trips(db_session, nobody.id, QUERY) == []  # nearest, but not near
    assert await search_trips(db_session, (await _user(db_session)).id, QUERY) == []  # no trips at all


# ── The HNSW index ─────────────────────────────────────────────────────────


def _sql(db_session, user_id) -> str:
    """The statement nearest_trips() runs, as SQL — for EXPLAIN."""
    distance = Embedding.vector.cosine_distance(QUERY)
    statement = (
        select(Trip.id, (1 - distance).label("similarity"))
        .join(Itinerary, Itinerary.trip_id == Trip.id)
        .join(Embedding, Embedding.itinerary_id == Itinerary.id)
        .where(Embedding.kind == SUMMARY, Trip.user_id == user_id)
        .order_by(distance)
        .limit(5)
    )
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_the_search_can_be_answered_from_the_hnsw_index_and_is_right_when_it_is(db_session):
    """Roadmap: cosine similarity search against the HNSW index.

    For a traveller with a handful of trips the planner picks those out first
    and sorts them — exact, and cheaper than any index. With reading the table
    and sorting both forbidden it has to walk the HNSW index, and the index
    hands back its nearest vectors whoever they belong to. Seventy trips of
    other people's are nearer than any of this traveller's: without an
    iterative scan the answer would come back empty.
    """
    me = await _user(db_session)
    for destination, similarity in (("Goa", 0.80), ("Varkala", 0.75), ("Hampi", 0.70)):
        await _trip(db_session, me, destination, similarity)
    crowd = await _user(db_session)
    for n in range(70):
        await _trip(db_session, crowd, f"Nearer {n}", 0.99 - n * 0.001)

    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    await db_session.execute(text("SET LOCAL enable_sort = off"))
    plan = "\n".join((await db_session.execute(text("EXPLAIN " + _sql(db_session, me.id)))).scalars())
    assert "Index Scan using ix_embeddings_summary_hnsw" in plan, plan[:300]

    assert _found(await nearest_trips(db_session, QUERY, me.id, limit=5)) == [
        ("Goa", 0.8),
        ("Varkala", 0.75),
        ("Hampi", 0.7),
    ]


@pytest.mark.asyncio
async def test_the_index_holds_summaries_only(db_session):
    definition = await db_session.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_embeddings_summary_hnsw'")
    )
    assert (
        "USING hnsw (vector vector_cosine_ops)" in definition and "WHERE ((kind)::text = 'summary'::text)" in definition
    )
    assert (
        await db_session.scalar(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_embeddings_vector_hnsw'"))
        == 0
    )


# ── The endpoints ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_similar_and_search_over_http(db_session):
    async with _stack(lambda *_: [flight(8_200.0)]) as (client, _, __):
        await _login(client)
        me = (await db_session.execute(select(User))).scalars().one()
        goa = await _trip(db_session, me, "Goa", 1.0)
        await _trip(db_session, me, "Varkala", 0.91)
        await _trip(db_session, me, "Hampi", 0.86)
        await _trip(db_session, me, "Leh", 0.60)
        fresh = await _trip(db_session, me, "Just planned", None)
        stranger = await _trip(db_session, await _user(db_session), "Their Goa", 0.99)
        embed = AsyncMock(return_value=QUERY)

        # the real cut-offs and a query that lands where QUERY is (the stack's stand-in embedder has its own)
        with (
            patch.object(search, "SIMILAR_FLOOR", 0.84),
            patch.object(search, "SEARCH_FLOOR", 0.58),
            patch.object(search, "SEARCH_WINDOW", 0.04),
            patch("app.api.routes.trips.embed_query", embed),
        ):
            # ── GET /trips/{id}/similar ──
            body = (await client.get(f"/trips/{goa.id}/similar")).json()
            assert body["status"] == "ready"
            assert [(r["trip"]["destination"], r["similarity"]) for r in body["results"]] == [
                ("Varkala", 0.91),
                ("Hampi", 0.86),
            ]
            first = body["results"][0]
            assert first["highlight"] == "Varkala Fort" and first["total_cost"] == 9_000
            assert first["trip"]["start_date"] == str(START) and uuid.UUID(first["itinerary_id"])
            assert len((await client.get(f"/trips/{goa.id}/similar?limit=1")).json()["results"]) == 1
            assert (await client.get(f"/trips/{goa.id}/similar?limit=6")).status_code == 422

            assert (await client.get(f"/trips/{fresh.id}/similar")).json() == {"status": "pending", "results": []}
            assert (await client.get(f"/trips/{stranger.id}/similar")).status_code == 404  # not this account's trip
            unplanned = (
                await client.post(
                    "/trips",
                    json={
                        "destination": "Agra",
                        "start_date": str(START),
                        "end_date": str(START + timedelta(days=1)),
                        "budget": 9000,
                    },
                )
            ).json()
            assert (await client.get(f"/trips/{unplanned['id']}/similar")).status_code == 404  # nothing to compare

            # ── GET /trips/search ──
            found = (await client.get("/trips/search", params={"q": "  by the   sea "})).json()
            assert found["query"] == "by the sea"
            # Goa is the query itself (1.0); Varkala and Hampi are not within 0.04 of that
            assert [(r["trip"]["destination"], r["similarity"]) for r in found["results"]] == [("Goa", 1.0)]
            embed.assert_awaited_once_with("by the sea")

            # the same words again are answered from the cache: the model is not asked twice
            again = (await client.get("/trips/search", params={"q": "BY THE SEA"})).json()
            assert [r["trip"]["destination"] for r in again["results"]] == ["Goa"] and embed.await_count == 1

            assert (await client.get("/trips/search", params={"q": "x"})).status_code == 422
            assert (await client.get("/trips/search")).status_code == 422
            embed.side_effect = RuntimeError("429 Too Many Requests")
            unavailable = await client.get("/trips/search", params={"q": "forts and palaces"})
            assert unavailable.status_code == 503
            assert unavailable.json()["detail"] == "Search is not available right now. Try again in a moment."

        client.headers.pop("Authorization")
        assert (await client.get("/trips/search", params={"q": "beach"})).status_code == 401
        assert (await client.get(f"/trips/{goa.id}/similar")).status_code == 401


# ── The whole pipeline ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_planned_beach_trip_is_like_another_beach_trip_and_not_like_a_mountain_one(db_session):
    """Roadmap acceptance, end to end: plan three trips; the two built from the same places find each other."""

    async def plan(client, destination: str, interests: list[str]) -> str:
        body = {
            "destination": destination,
            "start_date": str(START),
            "end_date": str(START + timedelta(days=2)),
            "budget": 50_000,
            "group_size": 2,
            "interests": interests,
        }
        trip_id = (await client.post("/trips", json=body)).json()["id"]
        assert (await client.post(f"/trips/{trip_id}/plan")).status_code == 202
        await _run_finished(client, trip_id, orchestrator_rows=1)
        return trip_id

    async with _stack(lambda *_: [flight(8_200.0)]) as (client, _, tools):
        tools["activities"].side_effect = lambda _name, params: (
            MOUNTAIN_ATTRACTIONS if params["destination"] == "Leh" else ATTRACTIONS
        )
        await _login(client)
        goa = await plan(client, "Goa", ["beach", "food"])
        varkala = await plan(client, "Varkala", ["beach"])
        leh = await plan(client, "Leh", ["trekking"])

        async def embedded():
            rows = (await db_session.execute(select(Embedding).where(Embedding.kind == SUMMARY))).scalars().all()
            return rows if len(rows) == 3 else None

        await _until(embedded, "three summary embeddings")
        kinds = (await db_session.execute(select(Embedding.kind))).scalars().all()
        assert sorted(kinds) == [FULL_TEXT] * 3 + [SUMMARY] * 3

        similar = (await client.get(f"/trips/{goa}/similar")).json()
        assert similar["status"] == "ready"
        assert [r["trip"]["id"] for r in similar["results"]] == [varkala]  # the other beach trip, and not the trek
        assert similar["results"][0]["highlight"] in {"Fort Aguada", "Basilica of Bom Jesus"}  # its most popular stop
        assert (await client.get(f"/trips/{leh}/similar")).json() == {"status": "ready", "results": []}

        beach = (await client.get("/trips/search", params={"q": "beach"})).json()["results"]
        assert {r["trip"]["id"] for r in beach} == {goa, varkala}
        monastery = (await client.get("/trips/search", params={"q": "monastery"})).json()["results"]
        assert [r["trip"]["id"] for r in monastery] == [leh]
        assert (await client.get("/trips/search", params={"q": "quarterly tax return"})).json()["results"] == []

        # a change to a trip saves a new itinerary, embedded in its turn: the trip is still found, once
        assert (await client.post(f"/trips/{varkala}/refine", json={"message": "a nicer hotel"})).status_code == 200
        await _run_finished(client, varkala, orchestrator_rows=2)

        async def embedded_again():
            rows = (await db_session.execute(select(Embedding).where(Embedding.kind == SUMMARY))).scalars().all()
            return rows if len(rows) == 4 else None

        await _until(embedded_again, "the refined itinerary's embedding")
        assert [r["trip"]["id"] for r in (await client.get(f"/trips/{goa}/similar")).json()["results"]] == [varkala]


# ── Migration 004 ──────────────────────────────────────────────────────────


def test_migration_004_re_queues_each_trips_latest_itinerary_and_comes_back_down():
    """Vectors written before Phase 23 cannot be told apart and are of the old summary: they are made again."""
    config = Config("alembic.ini")
    engine = create_engine(make_url(settings.DATABASE_URL).set(drivername="postgresql+psycopg2"))
    command.downgrade(config, "003")
    try:
        with engine.begin() as db:
            columns = (
                db.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'embeddings'"))
                .scalars()
                .all()
            )
            assert "kind" not in columns
            user, trip, old, latest, other_trip, lone = (str(uuid.uuid4()) for _ in range(6))
            db.execute(
                text("INSERT INTO users (id, email, hashed_password) VALUES (:id, 'm@example.com', 'x')"), {"id": user}
            )
            for trip_id in (trip, other_trip):
                db.execute(
                    text(
                        "INSERT INTO trips (id, user_id, destination, start_date, end_date, budget, group_size, status) VALUES (:id, :user, 'Goa', '2027-01-01', '2027-01-03', 9000, 1, 'completed')"
                    ),
                    {"id": trip_id, "user": user},
                )
            for itinerary, of_trip, created in (
                (old, trip, "2026-01-01"),
                (latest, trip, "2026-02-01"),
                (lone, other_trip, "2026-03-01"),
            ):
                db.execute(
                    text("INSERT INTO itineraries (id, trip_id, created_at) VALUES (:id, :trip, :created)"),
                    {"id": itinerary, "trip": of_trip, "created": created},
                )
            vector = "[" + ",".join(["0.1"] * 1536) + "]"
            for itinerary in (old, old, latest, latest):  # two vectors each, as Phase 14 wrote them
                db.execute(
                    text(
                        "INSERT INTO embeddings (id, itinerary_id, embedding_model, vector) VALUES (gen_random_uuid(), :id, 'gemini-embedding-001', :vector)"
                    ),
                    {"id": itinerary, "vector": vector},
                )
            # `lone` was already waiting to be embedded
            db.execute(
                text(
                    "INSERT INTO embeddings (id, itinerary_id, embedding_model) VALUES (gen_random_uuid(), :id, 'pending_retry')"
                ),
                {"id": lone},
            )

        command.upgrade(config, "head")
        with engine.begin() as db:
            rows = db.execute(
                text(
                    "SELECT itinerary_id::text, embedding_model, kind, vector IS NULL FROM embeddings ORDER BY itinerary_id"
                )
            ).all()
            # one pending row for each trip's latest itinerary — not two for the one that had one, none for the old version
            assert sorted(rows) == sorted([(latest, "pending_retry", None, True), (lone, "pending_retry", None, True)])
    finally:
        command.upgrade(config, "head")
        with engine.begin() as db:
            db.execute(text("TRUNCATE users CASCADE"))
        engine.dispose()

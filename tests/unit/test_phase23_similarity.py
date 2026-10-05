"""Phase 23 — similar trips and search: everything that needs no database.

  summary text   what kind of trip it is — and what it leaves out
  embedding      a stored text is a document, a typed one a query; a query is asked once
  rows           each of an itinerary's two vectors says which text it is of
  matches        the trip a card is named by; when the nearest trip is not near enough
  routes         GET /trips/search and GET /trips/{id}/similar — wiring, cache, errors
  recovery       pending embeddings are made one itinerary at a time

The ranking itself runs on pgvector: tests/integration/test_phase23_similarity_integration.py.
"""

import asyncio
import importlib.util
import json
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app import search
from app.models.embedding import Embedding
from app.models.itinerary import Itinerary
from app.models.trip import Trip
from app.search import Match, best_matches, highlight
from src.ai.embeddings import embedder
from src.ai.embeddings.embedder import (
    DOCUMENT,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    FULL_TEXT,
    QUERY,
    SUMMARY,
    EmbeddingNotConfigured,
    build_summary_text,
    places_and_kinds,
    write_embedding_rows,
)
from src.ai.embeddings.embedder import _embed_once as real_embed_once  # before the suite's fixture refuses it

START = date.today() + timedelta(days=30)
VECTOR = [0.5] * EMBEDDING_DIM


def _stop(name: str, category: str | None = None, rating: float | None = None) -> dict:
    return {"activity": name, "category": category, "rating": rating, "cost": 0}


GOA = {
    "days": [
        {"day": 1, "morning": _stop("Fort Aguada", "history", 7.0), "afternoon": _stop("Baga Beach", "beach", 2.0)},
        {"day": 2, "morning": _stop("Calangute Beach", "beach", 3.0), "evening": _stop("Explore the area")},
        {"day": 3, "morning": _stop("Old Market", "sightseeing", 1.0), "afternoon": _stop("Baga Beach", "beach", 2.0)},
    ],
    "total_cost": 17_200,
}


# ── The summary ────────────────────────────────────────────────────────────


def test_the_summary_says_what_kind_of_trip_it_is_and_nothing_about_its_size():
    text = build_summary_text("Goa", ["beach", "food"], GOA)
    assert text == (
        "beach and food trip to Goa. Kinds of places: beach, history. "
        "Places: Fort Aguada, Baga Beach, Calangute Beach, Old Market."
    )
    # the words that made a Goa beach trip look like a Ladakh trek of the same length and budget
    for word in ("days", "INR", "₹", "17,200", "budget", "mid-range", "luxury"):
        assert word not in text


def test_places_are_named_once_in_the_order_visited_and_kinds_from_the_most_common():
    names, kinds = places_and_kinds(GOA)
    assert names == ["Fort Aguada", "Baga Beach", "Calangute Beach", "Old Market"]  # no free time, no repeat
    assert kinds == ["beach", "history"]  # "sightseeing" says nothing about a trip
    assert places_and_kinds({}) == places_and_kinds({"days": None}) == ([], [])


def test_the_summary_keeps_to_three_kinds_and_six_places_and_cleans_up_interests():
    days = [
        {"morning": _stop(f"Place {n}", kind)} for n, kind in enumerate(["a", "a", "a", "b", "b", "c", "d", "e"], 1)
    ]
    text = build_summary_text("Hampi", ["  history ", "", None, 7], {"days": days})
    assert (
        text
        == "history trip to Hampi. Kinds of places: a, b, c. Places: Place 1, Place 2, Place 3, Place 4, Place 5, Place 6."
    )


# ── The embedding call ─────────────────────────────────────────────────────


@contextmanager
def _gemini(captured: dict, values=None, status: int = 200):
    """Stands in for httpx.AsyncClient: remembers the request, answers with a vector (or an HTTP error)."""

    class Client:
        def __init__(self, **options):
            captured["timeout"] = options.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, json=json, calls=captured.get("calls", 0) + 1)
            response = MagicMock(status_code=status)
            response.json.return_value = {"embedding": {"values": values or VECTOR}}
            if status != 200:
                response.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
            return response

    with (
        patch("src.ai.embeddings.embedder.httpx.AsyncClient", Client),
        patch.object(embedder.settings, "GOOGLE_API_KEY", "key"),
    ):
        yield


@pytest.mark.asyncio
async def test_a_stored_text_is_embedded_as_a_document_and_a_typed_one_as_a_query():
    sent: dict = {}
    with _gemini(sent), patch("src.ai.embeddings.embedder._embed_once", real_embed_once):
        assert await embedder._call_embed("beach trip to Goa.") == VECTOR
        assert sent["json"] == {
            "content": {"parts": [{"text": "beach trip to Goa."}]},
            "taskType": "RETRIEVAL_DOCUMENT",
            "outputDimensionality": 1536,
        }
        assert sent["url"].endswith(f"/models/{EMBEDDING_MODEL}:embedContent")
        assert sent["headers"] == {"x-goog-api-key": "key"}  # in a header: an error message never carries it

        assert await embedder.embed_query("beaches") == VECTOR
        assert (
            sent["json"]["taskType"] == "RETRIEVAL_QUERY" and sent["json"]["content"]["parts"][0]["text"] == "beaches"
        )
        assert sent["timeout"] == embedder.QUERY_TIMEOUT_S  # a request is waiting for it
    assert (DOCUMENT, QUERY) == ("RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY")


@pytest.mark.asyncio
async def test_a_query_is_asked_once_and_a_missing_key_is_said_at_once():
    sent: dict = {}
    with _gemini(sent, status=429), patch("src.ai.embeddings.embedder._embed_once", real_embed_once):
        with pytest.raises(RuntimeError, match="429"):
            await embedder.embed_query("beaches")
    assert sent["calls"] == 1  # no retries: the search answers 503 instead of making its caller wait

    with (
        patch.object(embedder.settings, "GOOGLE_API_KEY", ""),
        patch("src.ai.embeddings.embedder._embed_once", real_embed_once),
    ):
        with pytest.raises(EmbeddingNotConfigured):
            await embedder.embed_query("beaches")
        with pytest.raises(EmbeddingNotConfigured):  # …and a stored text does not spend a minute retrying it
            await asyncio.wait_for(embedder._call_embed("beach trip"), timeout=1)


# ── The rows ───────────────────────────────────────────────────────────────


def _session(rows: list | None = None) -> AsyncMock:
    db = AsyncMock()
    db.added = []
    db.add = db.added.append
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows or []
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_each_of_an_itinerarys_two_vectors_says_which_text_it_is_of():
    db, embedded = _session(), []

    async def embed(text: str, *task) -> list[float]:
        embedded.append(text)
        return [float(len(embedded))] * EMBEDDING_DIM

    itinerary_id = uuid.uuid4()
    with patch("src.ai.embeddings.embedder._call_embed", embed):
        await write_embedding_rows(itinerary_id, GOA, "Goa", ["beach", "food"], db)

    rows = {row.kind: row for row in db.added if isinstance(row, Embedding)}
    assert set(rows) == {FULL_TEXT, SUMMARY}
    assert all(row.itinerary_id == itinerary_id and row.embedding_model == EMBEDDING_MODEL for row in rows.values())
    # the summary row holds the vector of the summary text — the one with the traveller's interests in it
    summary_text = build_summary_text("Goa", ["beach", "food"], GOA)
    assert embedded == [embedder.build_full_text(GOA), summary_text]
    assert rows[SUMMARY].vector[0] == 2.0 and rows[FULL_TEXT].vector[0] == 1.0


@pytest.mark.asyncio
async def test_an_itinerary_is_embedded_with_its_trips_interests():
    from src.ai.utils.embeddings import generate_embeddings

    itinerary = SimpleNamespace(id=uuid.uuid4(), trip_id=uuid.uuid4(), structured_data=GOA, total_cost=17_200.0)
    trip = SimpleNamespace(destination="Goa", interests=["beach", "food"])
    db = AsyncMock()
    db.get = AsyncMock(side_effect=[itinerary, trip])
    with (
        patch("app.db.session.AsyncSessionLocal") as sessions,
        patch("src.ai.embeddings.embedder.write_embedding_rows", AsyncMock()) as write,
    ):
        sessions.return_value.__aenter__ = AsyncMock(return_value=db)
        sessions.return_value.__aexit__ = AsyncMock(return_value=False)
        await generate_embeddings(itinerary.id)
    assert write.await_args.kwargs == {
        "itinerary_id": itinerary.id,
        "structured_data": GOA,
        "destination": "Goa",
        "interests": ["beach", "food"],
        "db": db,
    }


# ── Matches ────────────────────────────────────────────────────────────────


def test_a_trip_is_named_by_its_most_popular_stop():
    assert highlight(GOA) == "Fort Aguada"  # rated 7: a heritage site
    level = {"days": [{"morning": _stop("First", rating=2), "afternoon": _stop("Second", rating=2)}]}
    assert highlight(level) == "First"  # all alike: the first
    unrated = {"days": [{"morning": _stop("Explore the area"), "evening": _stop("Only place", rating=True)}]}
    assert highlight(unrated) == "Only place"  # free time is no place; a rating that is not a number is no rating
    for nothing in (None, {}, {"days": []}, {"days": [{"morning": _stop("Explore the area")}]}):
        assert highlight(nothing) is None


def _matches(*similarities: float) -> list[Match]:
    return [Match(trip=MagicMock(), itinerary=MagicMock(), similarity=similarity) for similarity in similarities]


def test_search_keeps_the_trips_close_to_the_best_match():
    scores = lambda matches: [match.similarity for match in best_matches(matches)]  # noqa: E731
    assert scores(_matches(0.70, 0.68, 0.66, 0.65, 0.59)) == [0.70, 0.68, 0.66]  # within 0.04 of the best
    assert scores(_matches(0.60, 0.59, 0.57)) == [0.60, 0.59]  # …and never below the floor
    assert scores(_matches(0.5799, 0.57)) == []  # the nearest, but not near: nothing matched
    assert scores([]) == []
    assert (search.SEARCH_FLOOR, search.SEARCH_WINDOW, search.SIMILAR_FLOOR) == (0.58, 0.04, 0.84)
    assert (search.SIMILAR_LIMIT, search.SEARCH_LIMIT) == (5, 10)


# ── Routes ─────────────────────────────────────────────────────────────────


def _trip(owner: uuid.UUID, destination: str = "Goa") -> Trip:
    return Trip(
        id=uuid.uuid4(),
        user_id=owner,
        destination=destination,
        start_date=START,
        end_date=START + timedelta(days=2),
        budget=50_000.0,
        group_size=2,
        interests=["beach"],
        status="completed",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def _match(owner: uuid.UUID, destination: str, similarity: float) -> Match:
    trip = _trip(owner, destination)
    itinerary = Itinerary(id=uuid.uuid4(), trip_id=trip.id, structured_data=GOA, total_cost=17_200.0)
    return Match(trip=trip, itinerary=itinerary, similarity=similarity)


@contextmanager
def _client(trip: Trip | None = None, itinerary: Itinerary | None = None, cache=None):
    from app.api.deps import get_current_user, get_db, get_redis_or_none
    from app.main import app

    user_id = trip.user_id if trip else uuid.uuid4()
    db = AsyncMock()
    found_trip, found_itinerary = MagicMock(), MagicMock()
    found_trip.scalar_one_or_none.return_value = trip
    found_itinerary.scalar_one_or_none.return_value = itinerary
    db.execute = AsyncMock(side_effect=[found_trip, found_itinerary])

    async def _db():
        yield db

    app.dependency_overrides.update(
        {get_db: _db, get_redis_or_none: lambda: cache, get_current_user: lambda: MagicMock(id=user_id)}
    )
    try:
        yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), user_id
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_search_embeds_the_query_and_returns_the_matches_as_cards():
    owner = uuid.uuid4()
    found = [_match(owner, "Goa", 0.70123456), _match(owner, "Varkala", 0.68)]
    with (
        _client() as (client, user_id),
        patch("app.api.routes.trips.embed_query", AsyncMock(return_value=VECTOR)) as embed,
        patch("app.api.routes.trips.search_trips", AsyncMock(return_value=found)) as run,
    ):
        response = await client.get("/trips/search", params={"q": "  quiet   beaches "})

    assert response.status_code == 200
    embed.assert_awaited_once_with("quiet beaches")
    assert run.await_args.args[1:] == (user_id, VECTOR)  # the caller's own trips, and nobody else's
    body = response.json()
    assert body["query"] == "quiet beaches"
    assert [(r["trip"]["destination"], r["similarity"]) for r in body["results"]] == [
        ("Goa", 0.7012),
        ("Varkala", 0.68),
    ]
    first = body["results"][0]
    assert first["total_cost"] == 17_200 and first["highlight"] == "Fort Aguada"
    assert first["itinerary_id"] == str(found[0].itinerary.id) and first["trip"]["id"] == str(found[0].trip.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("q", ["", " ", "x", " y "])
async def test_search_needs_two_characters(q):
    with _client() as (client, _), patch("app.api.routes.trips.embed_query", AsyncMock()) as embed:
        assert (await client.get("/trips/search", params={"q": q})).status_code == 422
    embed.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [EmbeddingNotConfigured("no key"), RuntimeError("429 Too Many Requests"), TimeoutError()]
)
async def test_search_says_so_when_the_query_cannot_be_embedded(error):
    with _client() as (client, _), patch("app.api.routes.trips.embed_query", AsyncMock(side_effect=error)):
        response = await client.get("/trips/search", params={"q": "beaches"})
    assert response.status_code == 503
    assert response.json()["detail"] == "Search is not available right now. Try again in a moment."


class DictCache:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key], self.ttl[key] = value, ex


@pytest.mark.asyncio
async def test_a_querys_embedding_is_kept_for_a_day_under_the_models_name():
    cache = DictCache()
    with (
        _client(cache=cache) as (client, _),
        patch("app.api.routes.trips.embed_query", AsyncMock(return_value=VECTOR)) as embed,
        patch("app.api.routes.trips.search_trips", AsyncMock(return_value=[])) as run,
    ):
        for q in ("Quiet Beaches", "quiet beaches", "QUIET  BEACHES"):
            assert (await client.get("/trips/search", params={"q": q})).status_code == 200
        assert embed.await_count == 1  # the same words, however typed, ask the model once
        assert run.await_args.args[2] == VECTOR  # …and what comes back from the cache is the same vector
        assert (await client.get("/trips/search", params={"q": "forts"})).status_code == 200
        assert embed.await_count == 2

    (key, other) = cache.data
    assert key.startswith(f"search:query:{EMBEDDING_MODEL}:") and json.loads(cache.data[key]) == VECTOR
    assert cache.ttl[key] == 24 * 3600 and "quiet" not in key.lower()  # the words themselves are not in the key


@pytest.mark.asyncio
async def test_search_works_without_redis_and_with_a_redis_that_fails():
    broken = MagicMock(
        get=AsyncMock(side_effect=ConnectionError("down")), set=AsyncMock(side_effect=ConnectionError("down"))
    )
    for cache in (None, broken):
        with (
            _client(cache=cache) as (client, _),
            patch("app.api.routes.trips.embed_query", AsyncMock(return_value=VECTOR)),
            patch("app.api.routes.trips.search_trips", AsyncMock(return_value=[])),
        ):
            assert (await client.get("/trips/search", params={"q": "beaches"})).json() == {
                "query": "beaches",
                "results": [],
            }


@pytest.mark.asyncio
async def test_similar_is_ready_with_matches_or_pending_without_an_embedding():
    owner = uuid.uuid4()
    trip = _trip(owner)
    itinerary = Itinerary(id=uuid.uuid4(), trip_id=trip.id, structured_data=GOA, total_cost=17_200.0)

    with (
        _client(trip, itinerary) as (client, _),
        patch("app.api.routes.trips.similar_trips", AsyncMock(return_value=[_match(owner, "Varkala", 0.91)])) as run,
    ):
        body = (await client.get(f"/trips/{trip.id}/similar", params={"limit": 3})).json()
    assert body["status"] == "ready" and [r["trip"]["destination"] for r in body["results"]] == ["Varkala"]
    assert run.await_args.args[1] is trip and run.await_args.kwargs == {"limit": 3}

    with (
        _client(trip, itinerary) as (client, _),
        patch("app.api.routes.trips.similar_trips", AsyncMock(return_value=None)),
    ):
        assert (await client.get(f"/trips/{trip.id}/similar")).json() == {"status": "pending", "results": []}
    with (
        _client(trip, itinerary) as (client, _),
        patch("app.api.routes.trips.similar_trips", AsyncMock(return_value=[])),
    ):
        assert (await client.get(f"/trips/{trip.id}/similar")).json() == {"status": "ready", "results": []}


@pytest.mark.asyncio
async def test_similar_needs_the_callers_own_trip_and_a_plan_to_compare():
    trip = _trip(uuid.uuid4())
    with _client(None) as (client, _):
        assert (await client.get(f"/trips/{trip.id}/similar")).status_code == 404  # not theirs, or not there
    with _client(trip, None) as (client, _):
        response = await client.get(f"/trips/{trip.id}/similar")
    assert response.status_code == 404 and "itinerary" in response.json()["detail"].lower()
    with _client(trip, MagicMock()) as (client, _):
        assert (await client.get(f"/trips/{trip.id}/similar", params={"limit": 0})).status_code == 422


def test_search_is_declared_before_the_trip_routes():
    """ "search" is not a trip id: declared after /{trip_id}, the request would be refused as a malformed UUID."""
    from app.api.routes.trips import router

    paths = [route.path for route in router.routes]
    assert paths.index("/trips/search") < paths.index("/trips/{trip_id}")


# ── Startup recovery ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_embeddings_are_made_one_itinerary_at_a_time():
    """After migration 004 every trip is pending: all at once, the embedding API answers with 429s."""
    from app import main

    running, most_at_once, done = 0, 0, []

    async def generate(itinerary_id) -> None:
        nonlocal running, most_at_once
        running += 1
        most_at_once = max(most_at_once, running)
        await asyncio.sleep(0.01)
        running -= 1
        done.append(itinerary_id)

    ids = [uuid.uuid4() for _ in range(5)]
    with patch.object(main, "generate_embeddings", generate):
        await main._regenerate_embeddings(ids)
    assert done == ids and most_at_once == 1


# ── The experiment measures what is stored ─────────────────────────────────


def test_the_experiments_summary_is_the_applications():
    spec = importlib.util.spec_from_file_location("embedding_experiment", Path("scripts/embedding_experiment.py"))
    experiment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(experiment)

    fields, data = experiment.itinerary(experiment.TRIPS[0])
    assert experiment.CANDIDATES[experiment.SUMMARY](fields, data) == build_summary_text("Goa", ["beach", "food"], data)
    assert experiment.TASKS[1] == (DOCUMENT, QUERY)  # the pair the cut-offs were measured with
    assert len(experiment.TRIPS) == 12 and {trip[1] for trip in experiment.TRIPS} == {
        "beach",
        "mountains",
        "heritage",
        "spiritual",
        "nature",
    }

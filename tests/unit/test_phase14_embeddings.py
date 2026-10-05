"""
Unit tests for Phase 14 — Embedding generation.

All tests mock _call_embed (the single network seam) so nothing here
makes an OpenAI API call. The pattern mirrors how test_mcp_client.py patches
the Amadeus SDK.

Covers:
  - build_full_text (pure function)
  - build_summary_text (pure function)
  - write_embedding_rows success → 2 Embedding rows added
  - write_embedding_rows OpenAI failure → 1 pending_retry row added
  - write_embedding_rows cleans up stale pending_retry rows first
  - generate_embeddings end-to-end (mocked session + embedder)
  - generate_embeddings with missing itinerary (no exception raised)
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.embeddings.embedder import (
    EMBEDDING_MODEL,
    PENDING_RETRY_MODEL,
    build_full_text,
    build_summary_text,
    write_embedding_rows,
)

# ── Fixtures ────────────────────────────────────────────────────────────────

_STRUCTURED_DATA = {
    "days": [
        {
            "day": 1,
            "date": "2026-12-10",
            "morning": {"activity": "Fort Aguada", "cost": 0, "lat": 15.5, "lng": 73.7},
            "afternoon": {"activity": "Baga Beach", "cost": 500, "lat": 15.5, "lng": 73.8},
            "evening": {"activity": "Explore the area", "cost": 0},
            "hotel": {"name": "Goa Grand", "cost_per_night": 4500.0},
            "flight": None,
        },
        {
            "day": 2,
            "date": "2026-12-11",
            "morning": {"activity": "Anjuna Flea Market", "cost": 200},
            "afternoon": None,
            "evening": None,
            "hotel": {"name": "Goa Grand", "cost_per_night": 4500.0},
            "flight": None,
        },
    ],
    "total_cost": 12_700.0,
    "currency": "INR",
}

_FAKE_VECTOR = [0.1] * 1536


def _make_mock_db(stale_rows=None):
    """Return a configured AsyncMock session."""
    db = AsyncMock()
    added: list = []

    db.add = lambda obj: added.append(obj)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.delete = AsyncMock()

    stale = stale_rows or []
    stale_result = MagicMock()
    stale_result.scalars.return_value.all.return_value = stale
    db.execute = AsyncMock(return_value=stale_result)

    db._added = added
    return db


# ── build_full_text ──────────────────────────────────────────────────────────


def test_build_full_text_includes_all_named_activities():
    text = build_full_text(_STRUCTURED_DATA)
    assert "Fort Aguada" in text
    assert "Baga Beach" in text
    assert "Anjuna Flea Market" in text


def test_build_full_text_includes_hotel_names():
    text = build_full_text(_STRUCTURED_DATA)
    assert "Goa Grand" in text


def test_build_full_text_includes_explore_the_area_fallback():
    """Fallback phrase written by the builder should still appear in the text."""
    text = build_full_text(_STRUCTURED_DATA)
    assert "Explore the area" in text


def test_build_full_text_empty_data_returns_fallback():
    text = build_full_text({})
    assert text == "No itinerary data available."


def test_build_full_text_none_slots_are_skipped():
    data = {
        "days": [
            {
                "day": 1,
                "date": "2026-12-10",
                "morning": {"activity": "Fort Aguada", "cost": 0},
                "afternoon": None,
                "evening": None,
            }
        ]
    }
    text = build_full_text(data)
    assert "Fort Aguada" in text
    assert "None" not in text


# ── build_summary_text ────────────────────────────────────────────────────────


def test_build_summary_text_says_what_kind_of_trip_it_is():
    """Phase 23: interests, kinds of places, places — and nothing about the trip's length or cost."""
    data = {
        "days": [
            {
                "day": 1,
                "morning": {"activity": "Fort Aguada", "category": "history"},
                "afternoon": {"activity": "Baga Beach", "category": "beach"},
                "evening": {"activity": "Explore the area"},
            },
            {"day": 2, "morning": {"activity": "Anjuna Beach", "category": "beach"}},
        ]
    }
    text = build_summary_text("Goa", ["beach", "food"], data)
    assert text == (
        "beach and food trip to Goa. Kinds of places: beach, history. Places: Fort Aguada, Baga Beach, Anjuna Beach."
    )


def test_build_summary_text_names_six_places_and_never_free_time():
    text = build_summary_text("Goa", [], _STRUCTURED_DATA)
    # "Explore the area" is excluded from the summary (it's a fallback phrase)
    assert text == "Trip to Goa. Places: Fort Aguada, Baga Beach, Anjuna Flea Market."
    many = {"days": [{"morning": {"activity": f"Place {n}"}} for n in range(1, 10)]}
    assert build_summary_text("Goa", None, many).endswith(
        "Places: Place 1, Place 2, Place 3, Place 4, Place 5, Place 6."
    )


def test_build_summary_text_of_an_empty_itinerary_is_still_a_sentence():
    assert build_summary_text("Goa", ["beach"], {}) == "beach trip to Goa."
    assert build_summary_text("Goa", None, {"days": None}) == "Trip to Goa."


# ── write_embedding_rows (success path) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_write_embedding_rows_success_adds_two_rows():
    db = _make_mock_db()
    itinerary_id = uuid.uuid4()

    with patch(
        "src.ai.embeddings.embedder._call_embed",
        AsyncMock(return_value=_FAKE_VECTOR),
    ):
        await write_embedding_rows(
            itinerary_id=itinerary_id,
            structured_data=_STRUCTURED_DATA,
            destination="Goa",
            interests=["beach", "food"],
            db=db,
        )

    from app.models.embedding import Embedding

    embedding_rows = [r for r in db._added if isinstance(r, Embedding)]
    assert len(embedding_rows) == 2


@pytest.mark.asyncio
async def test_write_embedding_rows_success_uses_correct_model():
    db = _make_mock_db()

    with patch(
        "src.ai.embeddings.embedder._call_embed",
        AsyncMock(return_value=_FAKE_VECTOR),
    ):
        await write_embedding_rows(
            itinerary_id=uuid.uuid4(),
            structured_data=_STRUCTURED_DATA,
            destination="Goa",
            interests=["beach", "food"],
            db=db,
        )

    from app.models.embedding import Embedding

    for row in [r for r in db._added if isinstance(r, Embedding)]:
        assert row.embedding_model == EMBEDDING_MODEL


@pytest.mark.asyncio
async def test_write_embedding_rows_success_stores_vector():
    db = _make_mock_db()

    with patch(
        "src.ai.embeddings.embedder._call_embed",
        AsyncMock(return_value=_FAKE_VECTOR),
    ):
        await write_embedding_rows(
            itinerary_id=uuid.uuid4(),
            structured_data=_STRUCTURED_DATA,
            destination="Goa",
            interests=["beach", "food"],
            db=db,
        )

    from app.models.embedding import Embedding

    for row in [r for r in db._added if isinstance(r, Embedding)]:
        assert row.vector == _FAKE_VECTOR
        assert len(row.vector) == 1536


@pytest.mark.asyncio
async def test_write_embedding_rows_calls_openai_twice():
    """Two texts → two embed calls."""
    db = _make_mock_db()
    mock_embed = AsyncMock(return_value=_FAKE_VECTOR)

    with patch("src.ai.embeddings.embedder._call_embed", mock_embed):
        await write_embedding_rows(
            itinerary_id=uuid.uuid4(),
            structured_data=_STRUCTURED_DATA,
            destination="Goa",
            interests=["beach", "food"],
            db=db,
        )

    assert mock_embed.await_count == 2


# ── write_embedding_rows (failure path) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_write_embedding_rows_openai_failure_writes_pending_retry():
    db = _make_mock_db()
    itinerary_id = uuid.uuid4()

    with patch(
        "src.ai.embeddings.embedder._call_embed",
        AsyncMock(side_effect=Exception("OpenAI 429 rate limit")),
    ):
        await write_embedding_rows(
            itinerary_id=itinerary_id,
            structured_data=_STRUCTURED_DATA,
            destination="Goa",
            interests=["beach", "food"],
            db=db,
        )

    from app.models.embedding import Embedding

    embedding_rows = [r for r in db._added if isinstance(r, Embedding)]
    assert len(embedding_rows) == 1
    fallback = embedding_rows[0]
    assert fallback.embedding_model == PENDING_RETRY_MODEL
    assert fallback.itinerary_id == itinerary_id
    assert fallback.vector is None


@pytest.mark.asyncio
async def test_write_embedding_rows_failure_calls_rollback():
    db = _make_mock_db()

    with patch(
        "src.ai.embeddings.embedder._call_embed",
        AsyncMock(side_effect=Exception("timeout")),
    ):
        await write_embedding_rows(
            itinerary_id=uuid.uuid4(),
            structured_data=_STRUCTURED_DATA,
            destination="Goa",
            interests=None,
            db=db,
        )

    db.rollback.assert_called_once()


# ── write_embedding_rows (stale cleanup) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_write_embedding_rows_deletes_stale_pending_retry_rows():
    """Any pre-existing pending_retry rows must be deleted before writing fresh ones."""
    from app.models.embedding import Embedding

    stale = Embedding(
        itinerary_id=uuid.uuid4(),
        embedding_model=PENDING_RETRY_MODEL,
        vector=None,
    )
    db = _make_mock_db(stale_rows=[stale])

    with patch(
        "src.ai.embeddings.embedder._call_embed",
        AsyncMock(return_value=_FAKE_VECTOR),
    ):
        await write_embedding_rows(
            itinerary_id=uuid.uuid4(),
            structured_data=_STRUCTURED_DATA,
            destination="Goa",
            interests=["beach", "food"],
            db=db,
        )

    # db.delete was called for the stale row
    db.delete.assert_called_once_with(stale)


# ── generate_embeddings ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_embeddings_calls_write_embedding_rows():
    from src.ai.utils.embeddings import generate_embeddings

    fake_itinerary = MagicMock()
    fake_itinerary.id = uuid.uuid4()
    fake_itinerary.trip_id = uuid.uuid4()
    fake_itinerary.structured_data = _STRUCTURED_DATA
    fake_itinerary.total_cost = 12_700.0

    fake_trip = MagicMock()
    fake_trip.destination = "Goa"

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(side_effect=[fake_itinerary, fake_trip])
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.db.session.AsyncSessionLocal") as MockSL,
        patch("src.ai.embeddings.embedder._call_embed", AsyncMock(return_value=_FAKE_VECTOR)),
    ):
        MockSL.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        MockSL.return_value.__aexit__ = AsyncMock(return_value=False)

        # Mock the stale-row select query
        stale_result = MagicMock()
        stale_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=stale_result)
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.delete = AsyncMock()

        await generate_embeddings(fake_itinerary.id)

    # Two adds: one for each embedding row
    assert mock_db.add.call_count == 2


@pytest.mark.asyncio
async def test_generate_embeddings_missing_itinerary_does_not_raise():
    """If the itinerary is not in the DB (e.g. rolled back), swallow silently."""
    from src.ai.utils.embeddings import generate_embeddings

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=None)  # itinerary not found
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    with patch("app.db.session.AsyncSessionLocal") as MockSL:
        MockSL.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        MockSL.return_value.__aexit__ = AsyncMock(return_value=False)

        # Should not raise
        await generate_embeddings(uuid.uuid4())


@pytest.mark.asyncio
async def test_generate_embeddings_openai_failure_does_not_raise():
    """Even if OpenAI is down, generate_embeddings must not propagate the error."""
    from src.ai.utils.embeddings import generate_embeddings

    fake_itinerary = MagicMock()
    fake_itinerary.id = uuid.uuid4()
    fake_itinerary.trip_id = uuid.uuid4()
    fake_itinerary.structured_data = {}
    fake_itinerary.total_cost = None

    fake_trip = MagicMock()
    fake_trip.destination = "Goa"

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(side_effect=[fake_itinerary, fake_trip])
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.rollback = AsyncMock()
    mock_db.delete = AsyncMock()

    stale_result = MagicMock()
    stale_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=stale_result)

    with (
        patch("app.db.session.AsyncSessionLocal") as MockSL,
        patch(
            "src.ai.embeddings.embedder._call_embed",
            AsyncMock(side_effect=Exception("API unreachable")),
        ),
    ):
        MockSL.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        MockSL.return_value.__aexit__ = AsyncMock(return_value=False)

        # Must not raise
        await generate_embeddings(fake_itinerary.id)

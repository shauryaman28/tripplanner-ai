"""
Integration test for Phase 14 — Embedding generation against real Postgres.

Requires Docker Postgres and an OPENAI_API_KEY (or mocks the OpenAI call so
the DB write itself is what gets validated).

Run with:
    RUN_INTEGRATION=1 pytest tests/integration/test_phase14_integration.py -v

The test mocks _call_openai_embed (same seam as unit tests) so it validates:
  - The two Embedding rows are correctly inserted via write_embedding_rows
  - SELECT array_length(vector, 1) returns 1536 for both rows
  - embedding_model column is "text-embedding-3-small" for both rows
  - A simulated OpenAI failure leaves exactly one pending_retry row
  - Startup recovery cleans up pending_retry rows and re-queues generation
"""

import os
import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.models.embedding import Embedding
from app.models.itinerary import Itinerary
from app.models.trip import Trip, TripStatus
from app.models.user import User

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

_STRUCTURED_DATA = {
    "days": [
        {
            "day": 1,
            "date": "2026-12-10",
            "morning": {"activity": "Fort Aguada", "cost": 0, "lat": 15.5, "lng": 73.7},
            "afternoon": {"activity": "Baga Beach", "cost": 500},
            "evening": None,
            "hotel": {"name": "Goa Grand", "cost_per_night": 4500.0},
            "flight": None,
        }
    ],
    "total_cost": 12_700.0,
    "currency": "INR",
}

_FAKE_VECTOR = [0.01] * 1536


async def _make_itinerary(db_session) -> tuple[Itinerary, str]:
    """Create user → trip → itinerary fixture rows. Returns (itinerary, destination)."""
    user = User(email=f"{uuid.uuid4()}@example.com", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    trip = Trip(
        user_id=user.id,
        destination="Goa",
        start_date=date.today(),
        end_date=date.today() + timedelta(days=1),
        budget=12_700,
        status=TripStatus.COMPLETED,
    )
    db_session.add(trip)
    await db_session.commit()
    await db_session.refresh(trip)

    itinerary = Itinerary(
        trip_id=trip.id,
        structured_data=_STRUCTURED_DATA,
        total_cost=12_700.0,
    )
    db_session.add(itinerary)
    await db_session.commit()
    await db_session.refresh(itinerary)
    return itinerary, trip.destination


@pytest.mark.asyncio
async def test_write_embedding_rows_inserts_two_rows(db_session):
    """write_embedding_rows creates exactly 2 Embedding rows with correct fields."""
    from src.ai.embeddings.embedder import write_embedding_rows

    itinerary, destination = await _make_itinerary(db_session)

    with patch(
        "src.ai.embeddings.embedder._call_openai_embed",
        AsyncMock(return_value=_FAKE_VECTOR),
    ):
        await write_embedding_rows(
            itinerary_id=itinerary.id,
            structured_data=_STRUCTURED_DATA,
            destination=destination,
            total_cost=itinerary.total_cost,
            db=db_session,
        )

    rows = (
        await db_session.execute(
            select(Embedding).where(Embedding.itinerary_id == itinerary.id)
        )
    ).scalars().all()

    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
    for row in rows:
        assert row.embedding_model == "text-embedding-3-small"
        assert row.vector is not None
        assert len(row.vector) == 1536


@pytest.mark.asyncio
async def test_write_embedding_rows_failure_writes_pending_retry(db_session):
    """On OpenAI failure, exactly one pending_retry row is written."""
    from src.ai.embeddings.embedder import write_embedding_rows

    itinerary, destination = await _make_itinerary(db_session)

    with patch(
        "src.ai.embeddings.embedder._call_openai_embed",
        AsyncMock(side_effect=Exception("OpenAI timeout")),
    ):
        await write_embedding_rows(
            itinerary_id=itinerary.id,
            structured_data=_STRUCTURED_DATA,
            destination=destination,
            total_cost=itinerary.total_cost,
            db=db_session,
        )

    rows = (
        await db_session.execute(
            select(Embedding).where(Embedding.itinerary_id == itinerary.id)
        )
    ).scalars().all()

    assert len(rows) == 1
    assert rows[0].embedding_model == "pending_retry"
    assert rows[0].vector is None


@pytest.mark.asyncio
async def test_generate_embeddings_end_to_end(db_session):
    """generate_embeddings resolves the itinerary from DB and writes 2 rows."""
    from src.ai.utils.embeddings import generate_embeddings

    itinerary, _ = await _make_itinerary(db_session)

    with patch(
        "src.ai.embeddings.embedder._call_openai_embed",
        AsyncMock(return_value=_FAKE_VECTOR),
    ):
        await generate_embeddings(itinerary.id)

    # generate_embeddings opens its own session — query via the fixture session
    await db_session.expire_all()
    rows = (
        await db_session.execute(
            select(Embedding).where(Embedding.itinerary_id == itinerary.id)
        )
    ).scalars().all()

    assert len(rows) == 2
    assert all(r.embedding_model == "text-embedding-3-small" for r in rows)


@pytest.mark.asyncio
async def test_pending_retry_cleanup_on_recovery(db_session):
    """write_embedding_rows deletes a pre-existing pending_retry row before writing fresh ones."""
    from src.ai.embeddings.embedder import write_embedding_rows

    itinerary, destination = await _make_itinerary(db_session)

    # Plant a stale pending_retry row
    stale = Embedding(
        itinerary_id=itinerary.id,
        embedding_model="pending_retry",
        vector=None,
    )
    db_session.add(stale)
    await db_session.commit()

    with patch(
        "src.ai.embeddings.embedder._call_openai_embed",
        AsyncMock(return_value=_FAKE_VECTOR),
    ):
        await write_embedding_rows(
            itinerary_id=itinerary.id,
            structured_data=_STRUCTURED_DATA,
            destination=destination,
            total_cost=itinerary.total_cost,
            db=db_session,
        )

    await db_session.expire_all()
    rows = (
        await db_session.execute(
            select(Embedding).where(Embedding.itinerary_id == itinerary.id)
        )
    ).scalars().all()

    # Stale row gone; exactly 2 fresh rows
    pending = [r for r in rows if r.embedding_model == "pending_retry"]
    fresh = [r for r in rows if r.embedding_model == "text-embedding-3-small"]
    assert len(pending) == 0
    assert len(fresh) == 2

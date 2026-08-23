"""
Integration test for Phase 12 — build → evaluate → persist against a real
Postgres DB. Requires Docker. Run with:
    RUN_INTEGRATION=1 pytest tests/integration/

Mocks the Claude Haiku call only — everything else (DB writes, Evaluator's
deterministic checks, atomic commit) runs for real.
"""

import json
import os
import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.models.itinerary import Itinerary
from app.models.trip import Trip, TripStatus
from app.models.user import User
from src.ai.orchestrator.orchestrator import build_itinerary_node, evaluate_node, persist_node

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

FLIGHTS = [{"airline": "6E", "flight_number": "6E-204", "price_inr": 8200.0}]
HOTELS = [{"name": "Goa Grand", "price_per_night_inr": 4500.0}]
ATTRACTIONS = [{"name": "Fort Aguada", "category": "history"}]


def _good_draft_json(trip_start: str) -> str:
    return json.dumps({
        "days": [
            {
                "day": 1,
                "date": trip_start,
                "morning": {"activity": "Fort Aguada", "cost": 0, "lat": 15.5, "lng": 73.7},
                "afternoon": None,
                "evening": None,
                "hotel": {"name": "Goa Grand", "cost_per_night": 4500.0},
                "flight": None,
            }
        ],
        "total_cost": 8200.0 + 4500.0,
        "currency": "INR",
    })


async def _make_trip(db_session) -> Trip:
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
        status=TripStatus.PLANNING,
    )
    db_session.add(trip)
    await db_session.commit()
    await db_session.refresh(trip)
    return trip


@pytest.mark.asyncio
async def test_build_evaluate_persist_writes_itinerary_and_completes_trip(db_session):
    trip = await _make_trip(db_session)
    state = {
        "destination": "Goa",
        "start_date": str(trip.start_date),
        "end_date": str(trip.end_date),
        "budget": trip.budget,
        "group_size": 1,
        "flights": FLIGHTS,
        "hotels": HOTELS,
        "attractions": ATTRACTIONS,
        "evaluator_retry_count": 0,
        "db": db_session,
        "trip_id": trip.id,
        "publish_fn": None,
    }

    with patch(
        "src.ai.builder.builder._call_claude_llm",
        AsyncMock(return_value=_good_draft_json(str(trip.start_date))),
    ):
        s1 = await build_itinerary_node(state)
        s2 = await evaluate_node(s1)
        s3 = await persist_node(s2)

    assert s2["evaluator_verdict"]["passed"] is True
    assert s3["itinerary_id"] is not None

    rows = (await db_session.execute(select(Itinerary).where(Itinerary.trip_id == trip.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].total_cost == 12700.0

    refreshed = await db_session.get(Trip, trip.id)
    assert refreshed.status == TripStatus.COMPLETED


@pytest.mark.asyncio
async def test_persist_node_failure_leaves_neither_row_written(db_session):
    """Forced failure inside persist_node before commit → full rollback:
    no itinerary row, trip.status stays at its pre-persist value."""
    trip = await _make_trip(db_session)
    state = {
        "draft_itinerary": {"days": [], "total_cost": 0, "currency": "INR"},
        "db": db_session,
        "trip_id": trip.id,
    }

    with patch.object(db_session, "commit", AsyncMock(side_effect=RuntimeError("simulated failure"))):
        with pytest.raises(RuntimeError):
            await persist_node(state)

    await db_session.rollback()

    rows = (await db_session.execute(select(Itinerary).where(Itinerary.trip_id == trip.id))).scalars().all()
    assert len(rows) == 0

    refreshed = await db_session.get(Trip, trip.id)
    assert refreshed.status == TripStatus.PLANNING

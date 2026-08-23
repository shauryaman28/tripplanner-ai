"""
Integration test for Phase 11 — Evaluator run logging & retry chain
reconstruction against a real Postgres DB.

Requires Docker Postgres. Run with:
    RUN_INTEGRATION=1 pytest tests/integration/

Since ItineraryBuilder (Phase 12) doesn't exist yet, this simulates a
retry loop by hand: log a flight_agent row, run the Evaluator against a
deliberately bad draft, repeat until the retry cap, then confirm
get_retry_chain() reconstructs the correct ordered, per-agent-numbered
timeline — the Phase 11 Dev B acceptance criterion.
"""

import os
import uuid
from datetime import date, timedelta

import pytest

from app.models.trip import Trip, TripStatus
from app.models.user import User
from src.ai.agents.evaluator import MAX_EVALUATOR_RETRIES, EvaluatorAgent
from src.ai.utils.run_logger import get_retry_chain, log_agent_run

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

BAD_DRAFT = {
    "days": [
        {
            "day": 1,
            "date": "2099-01-01",  # always outside any real trip window
            "morning": {"activity": "Nonexistent Museum", "cost": 100},
        }
    ],
    "total_cost": 999_999,
    "currency": "INR",
}


async def _make_trip(db_session) -> Trip:
    user = User(email=f"{uuid.uuid4()}@example.com", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    trip = Trip(
        user_id=user.id,
        destination="Goa",
        start_date=date.today(),
        end_date=date.today() + timedelta(days=5),
        budget=20_000,
        status=TripStatus.PENDING,
    )
    db_session.add(trip)
    await db_session.commit()
    await db_session.refresh(trip)
    return trip


@pytest.mark.asyncio
async def test_evaluator_writes_row_between_agent_runs_and_retry_visible(db_session):
    """flight_agent → evaluator(failed) → flight_agent(retry) → evaluator(failed)
    ... until the retry cap. get_retry_chain() must show correct ordering and
    per-agent attempt numbers."""
    trip = await _make_trip(db_session)
    evaluator = EvaluatorAgent()

    retry_count = 0
    for _ in range(MAX_EVALUATOR_RETRIES + 1):
        await log_agent_run(
            db=db_session,
            trip_id=trip.id,
            agent_name="flight_agent",
            input={},
            output={"flights": []},
            duration_ms=10,
            status="completed",
        )
        await evaluator.run(
            draft=BAD_DRAFT,
            trip_start=str(trip.start_date),
            trip_end=str(trip.end_date),
            expected_budget_total=trip.budget,
            attractions=[],
            retry_count=retry_count,
            db=db_session,
            trip_id=trip.id,
        )
        retry_count += 1

    chain = await get_retry_chain(db_session, trip.id)

    evaluator_rows = [r for r in chain if r["agent_name"] == "evaluator"]
    flight_rows = [r for r in chain if r["agent_name"] == "flight_agent"]

    assert len(evaluator_rows) == MAX_EVALUATOR_RETRIES + 1
    assert len(flight_rows) == MAX_EVALUATOR_RETRIES + 1
    assert all(r["status"] == "failed" for r in evaluator_rows)
    assert [r["attempt"] for r in evaluator_rows] == list(range(1, MAX_EVALUATOR_RETRIES + 2))
    # evaluator row for a given attempt comes right after that attempt's flight_agent row
    assert chain[0]["agent_name"] == "flight_agent"
    assert chain[1]["agent_name"] == "evaluator"

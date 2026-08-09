import os
import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.models.agent_run import AgentRun
from app.models.trip import Trip, TripStatus
from app.models.user import User
from src.ai.mcp_server.models import Flight

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

FUTURE = (date.today() + timedelta(days=30)).isoformat()


@pytest.mark.asyncio
async def test_flight_agent_run_writes_one_agent_run_row(db_session):
    """FlightAgent.run() + log_agent_run() together write exactly one
    correctly-shaped row to agent_runs — the Phase 6 Dev B done criterion."""
    from src.ai.agents.flight_agent import FlightAgent

    # 1. real user + trip rows so the trip_id FK is valid
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

    # 2. mock call_tool so the test needs no live MCP server
    fake_flight = Flight(
        airline="6E", flight_number="6E-204",
        departure=f"{FUTURE}T06:00:00", arrival=f"{FUTURE}T08:15:00",
        duration_mins=135, price_inr=4200.0, stops=0,
    ).model_dump()

    with patch("src.ai.agents.flight_agent.call_tool", AsyncMock(return_value=[fake_flight])):
        agent = FlightAgent()
        result = await agent.run(
            {
                "origin": "DEL", "destination": "GOI",
                "date": FUTURE, "budget": 20_000, "passengers": 1,
            },
            db=db_session,
            trip_id=trip.id,
        )

    # 3. agent returned correct data
    assert result["error"] is None
    assert len(result["flights"]) == 1
    assert result["flights"][0]["airline"] == "6E"

    # 4. exactly one agent_runs row, correctly shaped
    rows = (
        await db_session.execute(
            select(AgentRun).where(AgentRun.trip_id == trip.id)
        )
    ).scalars().all()

    assert len(rows) == 1
    run = rows[0]
    assert run.agent_name == "flight_agent"
    assert run.status == "completed"
    assert run.duration_ms is not None and run.duration_ms >= 0
    assert run.output["flights"][0]["airline"] == "6E"
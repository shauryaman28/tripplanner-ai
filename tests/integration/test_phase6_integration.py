import os
import uuid
import pytest
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import User, Trip, AgentRun
from src.ai.mcp_client.client import call_tool, close_session
from src.ai.utils.run_logger import log_agent_run, timed_run

# Gate test using the pytest integration decorator/mark
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="RUN_INTEGRATION=1 is not set"
)


@pytest.mark.asyncio
async def test_mcp_client_and_run_logger_integration(db_session: AsyncSession):
    """Integration test verifying call_tool and log_agent_run.
    
    Creates a mock trip, runs estimate_budget via call_tool inside a timed_run,
    and logs the agent run in the database.
    """
    # 1. Create a user
    user = User(email="dev_b_test@example.com", hashed_password="hashed_dummy_password")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    # 2. Create a trip
    trip = Trip(
        user_id=user.id,
        destination="Goa",
        start_date="2025-12-10",
        end_date="2025-12-17",
        budget=50000.0,
        interests=["beach", "food"],
        status="pending"
    )
    db_session.add(trip)
    await db_session.commit()
    await db_session.refresh(trip)

    # 3. Simulate agent node execution
    tool_params = {
        "flights": 10000.0,
        "hotels": 2000.0,
        "days": 7,
        "daily_spend": 1500.0
    }

    async with timed_run() as timer:
        tool_result = await call_tool("estimate_budget", tool_params)
    
    # Close session to clean up the subprocess
    await close_session()

    assert not isinstance(tool_result, Exception)
    assert isinstance(tool_result, dict)
    assert "total" in tool_result

    # Log run as completed using the test db_session
    run_record = await log_agent_run(
        db=db_session,
        trip_id=trip.id,
        agent_name="flight_agent",
        input={"origin": "DEL", "destination": "GOI", "budget": 20000.0},
        output=tool_result,
        duration_ms=timer.duration_ms,
        status="completed"
    )

    assert run_record.id is not None
    assert run_record.status == "completed"
    assert run_record.output == tool_result
    assert run_record.duration_ms == timer.duration_ms

    # 4. Verify in DB using the test db_session
    query = select(AgentRun).where(AgentRun.id == run_record.id)
    result = await db_session.execute(query)
    db_record = result.scalar_one_or_none()
    
    assert db_record is not None
    assert db_record.agent_name == "flight_agent"
    assert db_record.status == "completed"
    assert db_record.output == tool_result
    assert db_record.duration_ms == timer.duration_ms

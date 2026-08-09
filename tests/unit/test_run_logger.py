import pytest
import uuid
import asyncio
from unittest.mock import AsyncMock
from src.ai.utils.run_logger import log_agent_run, timed_run
from app.models.agent_run import AgentRun
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_log_agent_run_create():
    """Test log_agent_run successfully inserts a new AgentRun row."""
    trip_id = uuid.uuid4()
    
    mock_session = AsyncMock(spec=AsyncSession)
    
    # Track model added to session
    added_objs = []
    def mock_add(obj):
        added_objs.append(obj)
    mock_session.add = mock_add
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()

    res = await log_agent_run(
        db=mock_session,
        trip_id=trip_id,
        agent_name="flight_agent",
        input={"origin": "DEL"},
        output={"flights": []},
        duration_ms=150,
        status="completed"
    )
    
    assert len(added_objs) == 1
    db_run = added_objs[0]
    assert isinstance(db_run, AgentRun)
    assert db_run.trip_id == trip_id
    assert db_run.agent_name == "flight_agent"
    assert db_run.status == "completed"
    assert db_run.input == {"origin": "DEL"}
    assert db_run.output == {"flights": []}
    assert db_run.duration_ms == 150
    assert res == db_run
    
    mock_session.commit.assert_called_once()
    mock_session.refresh.assert_called_once_with(res)


@pytest.mark.asyncio
async def test_timed_run_context_manager():
    """Test timed_run context manager measures duration in milliseconds."""
    async with timed_run() as timer:
        await asyncio.sleep(0.05)
        
    assert timer.duration_ms >= 40  # Allow slight timing variation

"""Agent run logger — Phase 6 Dev B.

Writes one row to agent_runs per agent execution. The caller (a FastAPI
route or agent wrapper) passes in its existing AsyncSession so the log
write participates in the same transaction as the rest of the request —
no separate connection opened here.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

try:
    from app.models.agent_run import AgentRun
except ImportError:
    from src.backend.app.models.agent_run import AgentRun


async def log_agent_run(
    db: AsyncSession,
    trip_id: uuid.UUID,
    agent_name: str,
    input: dict,
    output: dict,
    duration_ms: int,
    status: str = "completed",
) -> AgentRun:
    """Write one agent_runs row and return it."""
    run = AgentRun(
        trip_id=trip_id,
        agent_name=agent_name,
        status=status,
        input=input,
        output=output,
        duration_ms=duration_ms,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


@asynccontextmanager
async def timed_run():
    """Async context manager that measures wall-clock duration in ms.

    Usage:
        async with timed_run() as timer:
            result = await do_work()
        print(timer.duration_ms)
    """
    class _Timer:
        duration_ms: int = 0

    t = _Timer()
    start = time.monotonic()
    try:
        yield t
    finally:
        t.duration_ms = int((time.monotonic() - start) * 1000)

"""Agent run logger — Phase 6 Dev B, extended Phase 11 Dev B.

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
from sqlmodel import select

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


async def get_retry_chain(db: AsyncSession, trip_id: uuid.UUID) -> list[dict]:
    """Reconstruct the evaluation + retry timeline for a trip — Phase 11 Dev B.

    Returns all agent_runs rows for the trip ordered by created_at, each
    tagged with a per-agent-name running attempt counter (1-indexed), so
    callers can see e.g. "flight_agent attempt 2" followed by "evaluator
    attempt 2" without needing a dedicated retry_count column.

    Example:
        [
          {"agent_name": "flight_agent", "attempt": 1, "status": "completed", ...},
          {"agent_name": "evaluator",    "attempt": 1, "status": "failed",    ...},
          {"agent_name": "flight_agent", "attempt": 2, "status": "completed", ...},
          {"agent_name": "evaluator",    "attempt": 2, "status": "completed", ...},
        ]
    """
    result = await db.execute(
        select(AgentRun).where(AgentRun.trip_id == trip_id).order_by(AgentRun.created_at.asc())
    )
    rows = result.scalars().all()

    attempt_counts: dict[str, int] = {}
    chain: list[dict] = []
    for row in rows:
        attempt_counts[row.agent_name] = attempt_counts.get(row.agent_name, 0) + 1
        chain.append(
            {
                "id": row.id,
                "agent_name": row.agent_name,
                "attempt": attempt_counts[row.agent_name],
                "status": row.status,
                "output": row.output,
                "duration_ms": row.duration_ms,
                "created_at": row.created_at,
            }
        )
    return chain

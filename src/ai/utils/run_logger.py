"""Agent run logger — Phase 6 Dev B, extended Phase 11 Dev B, Phase 15.

Phase 15 addition: `turn` parameter on log_agent_run() so every row
written during a refinement pass is tagged with its conversation turn.
Defaults to 1, so all existing callers need no change.
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
    turn: int = 1,
) -> AgentRun:
    """Write one agent_runs row and return it.

    The `turn` parameter (added in Phase 15) defaults to 1 so all
    existing callers — FlightAgent, HotelAgent, ActivitiesAgent,
    ItineraryBuilder, EvaluatorAgent, OrchestratorAgent — require no
    changes for their first-turn behaviour.
    """
    run = AgentRun(
        trip_id=trip_id,
        agent_name=agent_name,
        status=status,
        input=input,
        output=output,
        duration_ms=duration_ms,
        turn=turn,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


@asynccontextmanager
async def timed_run():
    """Async context manager that measures wall-clock duration in ms."""

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

    Returns all agent_runs rows ordered by created_at, each tagged with a
    per-agent-name running attempt counter (1-indexed).
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
                "turn": row.turn,
            }
        )
    return chain

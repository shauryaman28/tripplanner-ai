"""Agent run logger — Phase 6 Dev B, extended Phase 11 Dev B, Phase 15, Phase 26.

Every agent and decision node writes one agent_runs row through
log_agent_run(); `turn` tags the row with its conversation turn (Phase 15).

Phase 26: timed_run() also counts what the models were asked while it was
open, and a row logged with that `usage` carries the tokens and what they cost
(src/ai/usage.py). A row for work that asked no model has neither. When a
trip's rows add up to more than LLM_COST_ALERT_USD, that is logged — once, as
the total passes it.

Phase 29: timed_run("flight_agent") names whose work it measures — the name its
row is logged under. The model calls made meanwhile are counted for that agent,
and the work is a span of the trace under way: every agent_runs row a span.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from src.ai.usage import Usage, metered
from src.telemetry import traces

try:
    from app.core.config import settings
    from app.models.agent_run import AgentRun
except ImportError:
    from src.backend.app.core.config import settings
    from src.backend.app.models.agent_run import AgentRun

logger = logging.getLogger(__name__)


# One planning run shares a single AsyncSession, and the orchestrator fans
# HotelAgent and ActivitiesAgent out concurrently. A session cannot be used by
# two coroutines at once, so writes on the same session are serialised.
_session_locks: WeakKeyDictionary[AsyncSession, asyncio.Lock] = WeakKeyDictionary()


async def log_agent_run(
    db: AsyncSession,
    trip_id: uuid.UUID,
    agent_name: str,
    input: dict,
    output: dict,
    duration_ms: int,
    status: str = "completed",
    turn: int = 1,
    usage: Usage | None = None,
) -> AgentRun:
    """Write one agent_runs row and return it.

    `usage` is what the models were asked while the work ran (timed_run's
    `timer.usage`): its tokens and their cost go on the row. Left out, or with
    no model asked, both stay NULL — "no model was asked" is not "0 tokens".
    """
    run = AgentRun(
        trip_id=trip_id,
        agent_name=agent_name,
        status=status,
        input=input,
        output=output,
        duration_ms=duration_ms,
        turn=turn,
        token_count=usage.token_count if usage is not None else None,
        cost_usd=usage.cost_usd if usage is not None else None,
    )
    async with _session_locks.setdefault(db, asyncio.Lock()):
        db.add(run)
        await db.commit()
        await db.refresh(run)
        if run.cost_usd:
            await _warn_if_over(db, trip_id, run.cost_usd)
    return run


async def _warn_if_over(db: AsyncSession, trip_id: uuid.UUID, added: float) -> None:
    """Log once when a trip's model cost passes LLM_COST_ALERT_USD: on the row that takes it over.

    A plan costs a fraction of a cent, so a trip over the limit is a loop — an
    evaluator sending a plan back again and again — or a price that changed.
    Never raises: a row has been written, and that is what the caller asked for.
    """
    limit = settings.LLM_COST_ALERT_USD
    if not limit or limit <= 0:
        return
    try:
        total = float(
            await db.scalar(select(func.coalesce(func.sum(AgentRun.cost_usd), 0.0)).where(AgentRun.trip_id == trip_id))
        )
        runs = await db.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.trip_id == trip_id, AgentRun.cost_usd.is_not(None))
        )
    except Exception:
        logger.warning("Could not add up the model cost of trip %s", trip_id, exc_info=True)
        return
    if total > limit >= total - added:
        logger.warning(
            "[COST ALERT] trip %s: the models have cost $%.4f over %s — past the %s one trip is expected to stay under",
            trip_id,
            total,
            "1 run" if runs == 1 else f"{runs} runs",
            _dollars(limit),
        )


def _dollars(amount: float) -> str:
    """A limit as it was set: "$0.10", and "$0.002" — never a limit below a cent rounded to "$0.00"."""
    whole, _, rest = f"{amount:.6f}".rstrip("0").partition(".")
    return f"${whole}.{rest.ljust(2, '0')}"


@asynccontextmanager
async def timed_run(agent: str | None = None):
    """Measures a stretch of work: how long it took, and what it asked of the models (`timer.usage`, Phase 26).

    `agent` is the name the work is logged under (Phase 29): its model calls are counted for it,
    and it is a span of that name. Without one, the work is timed and metered and nothing more.
    """

    class _Timer:
        duration_ms: int = 0
        usage: Usage

    t = _Timer()
    start = time.monotonic()
    with metered(agent) as usage:
        t.usage = usage
        try:
            if agent is None:
                yield t
            else:
                with traces.span(agent, {"agent.name": agent}):
                    yield t
        finally:
            t.duration_ms = int((time.monotonic() - start) * 1000)


async def get_retry_chain(db: AsyncSession, trip_id: uuid.UUID) -> list[dict]:
    """Reconstruct the evaluation + retry timeline for a trip — Phase 11 Dev B.

    Returns all agent_runs rows ordered by created_at, each tagged with a
    per-agent-name running attempt counter (1-indexed).
    """
    result = await db.execute(select(AgentRun).where(AgentRun.trip_id == trip_id).order_by(AgentRun.created_at.asc()))
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

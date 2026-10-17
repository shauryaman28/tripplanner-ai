"""
Operational endpoints.

GET /admin/embedding-health   embedding pipeline backlog (Phase 14)
GET /admin/cost-summary       tokens and model cost, by agent and by day (Phase 26)
GET /admin/sentry-test        fails on purpose, to see error reporting work (Phase 27; needs OPS_TOKEN)
GET /admin/health-deep        every dependency asked to do its work, and what each said (Phase 34; the
                              operator's: OPS_TOKEN, as for GET /metrics)

Any authenticated user may call the first two for now — there is no admin role until
the admin dashboard phase. Both report on the whole system, not on the
caller's own trips; neither shows anything of anyone's trip.
"""

import hmac
from datetime import date, datetime, time, timedelta, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import Date, cast, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.api.deps import get_current_user, get_redis_dep, may_check_health
from app.core import deep_health, error_reporting
from app.core.config import settings
from app.db.session import get_db
from app.models.agent_run import AgentRun
from app.models.embedding import Embedding
from app.models.user import User
from app.schemas.cost import AgentCost, CostLine, CostSummary, DayCost
from app.schemas.text import TOKEN_CHARS
from src.ai.embeddings.embedder import PENDING_RETRY_MODEL
from src.ai.mcp_server.config import mcp_settings
from src.ai.utils.embeddings import EMBEDDINGS_PENDING_KEY

router = APIRouter(prefix="/admin", tags=["admin"])


async def embedding_backlog(db: AsyncSession, redis: aioredis.Redis) -> dict[str, int]:
    """`in_flight` = jobs running right now (Redis counter); `pending_retry` = itineraries
    whose embedding failed and will be retried on the next startup.

    Phase 34: also what the `embedding_queue_depth` gauge reports (main.py reads it every
    15 s) — the same two numbers on the dashboard as here.
    """
    in_flight = max(int(await redis.get(EMBEDDINGS_PENDING_KEY) or 0), 0)
    pending_retry = await db.scalar(
        select(func.count()).select_from(Embedding).where(Embedding.embedding_model == PENDING_RETRY_MODEL)
    )
    return {"in_flight": in_flight, "pending_retry": pending_retry or 0}


@router.get("/embedding-health")
async def embedding_health(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_dep),
) -> dict:
    backlog = await embedding_backlog(db, redis)
    return {"status": "degraded" if backlog["pending_retry"] else "ok", **backlog}


# ── GET /admin/health-deep ─────────────────────────────────────────────────

_scrub = error_reporting.Scrubber(error_reporting.secrets_of(settings, mcp_settings)).text


@router.get("/health-deep")
async def health_deep(response: Response, _: None = Depends(may_check_health)) -> dict:
    """Phase 34 — Postgres, Redis, the tool server and LangSmith, each asked to do its work once.

    Postgres runs `SELECT 1` through the app's pool, Redis answers `PING`, the tool server answers
    `estimate_budget` (started first if it is not running), and LangSmith is shown the key. Each
    has a deadline of its own and they are asked together, so one that hangs is reported as
    hanging and the answer still comes within ten seconds. 503 when something a plan needs is
    failing; LangSmith failing is `degraded`, and 200 — plans are made without it. What each
    check found is in `dependencies` (app/core/deep_health.py says what each status means).

    The operator's, like GET /metrics: with `OPS_TOKEN` set it answers only to it (as
    `Authorization: Bearer …` or `X-Ops-Token`), and a deployment without one answers nobody.
    """
    found = await deep_health.report(settings, _scrub)
    if found["status"] == "failing":
        response.status_code = 503
    return found


# ── GET /admin/cost-summary ────────────────────────────────────────────────

DEFAULT_SINCE_DAYS = 30


def _line(runs: int | None, tokens: int | None, cost: float | None, unpriced: int | None) -> dict:
    return {
        "runs": runs or 0,
        "tokens": int(tokens or 0),
        "cost_usd": round(float(cost or 0.0), 6),
        "unpriced_tokens": int(unpriced or 0),
    }


@router.get("/cost-summary", response_model=CostSummary)
async def cost_summary(
    since: date | None = Query(None, description="First day to count, e.g. 2026-10-01. Default: 30 days ago."),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CostSummary:
    """Phase 26 — what the models were asked since a day, and what that costs: in all, by agent, by day.

    Counted from `agent_runs`: every run that asked a model carries its tokens
    and their list price (src/ai/usage.py). Runs that asked none — searches,
    the budget check, the evaluator — are not in it. The dollars are list
    prices: on a free tier nothing was charged.

    `unpriced_tokens`: tokens of a model with no price on record. They are
    counted in `tokens` and missing from `cost_usd`.
    """
    first_day = since or (datetime.now(timezone.utc).date() - timedelta(days=DEFAULT_SINCE_DAYS))
    asked_a_model = (AgentRun.token_count.is_not(None), AgentRun.created_at >= datetime.combine(first_day, time.min))
    figures = (
        func.count(),
        func.sum(AgentRun.token_count),
        func.sum(AgentRun.cost_usd),
        func.sum(AgentRun.token_count).filter(AgentRun.cost_usd.is_(None)),
    )

    total = (await db.execute(select(*figures).where(*asked_a_model))).one()
    by_agent = (
        await db.execute(
            select(AgentRun.agent_name, *figures)
            .where(*asked_a_model)
            .group_by(AgentRun.agent_name)
            .order_by(func.sum(AgentRun.cost_usd).desc().nulls_last(), AgentRun.agent_name)
        )
    ).all()
    day = cast(AgentRun.created_at, Date)
    by_date = (await db.execute(select(day, *figures).where(*asked_a_model).group_by(day).order_by(day))).all()

    return CostSummary(
        since=first_day,
        total=CostLine(**_line(*total)),
        by_agent=[AgentCost(agent=name, **_line(*rest)) for name, *rest in by_agent],
        by_date=[DayCost(date=on, **_line(*rest)) for on, *rest in by_date],
    )


# ── GET /admin/sentry-test ─────────────────────────────────────────────────


class DeliberateError(RuntimeError):
    """Raised on purpose, by GET /admin/sentry-test — never a sign that something is wrong."""


@router.get("/sentry-test")
async def sentry_test(
    marker: str = Query(
        "no-marker", max_length=64, pattern=r"^[A-Za-z0-9_-]{1,64}$", description="Put in the error, to find it by."
    ),
    x_ops_token: str | None = Header(None, max_length=TOKEN_CHARS),
) -> None:
    """Phase 27 — fail on purpose, so that error reporting can be seen to work.

    Answers 500, and the error — `DeliberateError: deliberate test error <marker>`
    — is in Sentry a moment later (search for the marker). It is not for anyone
    who happens to have an account: every call is an error against the Sentry
    project's quota. So it takes the deployment's `OPS_TOKEN`, as the
    X-Ops-Token header; without one configured, or with the wrong one, the route
    answers as if it were not there.
    """
    expected = settings.OPS_TOKEN
    if not expected or not hmac.compare_digest((x_ops_token or "").encode(), expected.encode()):
        raise HTTPException(status_code=404, detail="Not Found")
    raise DeliberateError(f"deliberate test error {marker}")

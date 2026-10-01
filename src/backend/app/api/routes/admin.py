"""
Operational endpoints.

GET /admin/embedding-health   embedding pipeline backlog (Phase 14)

Any authenticated user may call these for now — there is no admin role until
the admin dashboard phase.
"""

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.api.deps import get_current_user, get_redis_dep
from app.db.session import get_db
from app.models.embedding import Embedding
from app.models.user import User
from src.ai.embeddings.embedder import PENDING_RETRY_MODEL
from src.ai.utils.embeddings import EMBEDDINGS_PENDING_KEY

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/embedding-health")
async def embedding_health(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_dep),
) -> dict:
    """`in_flight` = jobs running right now (Redis counter); `pending_retry` = itineraries
    whose embedding failed and will be retried on the next startup."""
    in_flight = max(int(await redis.get(EMBEDDINGS_PENDING_KEY) or 0), 0)
    pending_retry = await db.scalar(
        select(func.count()).select_from(Embedding).where(Embedding.embedding_model == PENDING_RETRY_MODEL)
    )
    return {
        "status": "degraded" if pending_retry else "ok",
        "in_flight": in_flight,
        "pending_retry": pending_retry or 0,
    }

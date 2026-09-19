"""
FastAPI application entry point.

  uvicorn app.main:app --reload           (local dev)
  docker compose up backend               (Docker)
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.auth import router as auth_router
from app.api.routes.health import router as health_router
from app.api.routes.trips import router as trips_router
from app.api.routes.users import router as users_router
from app.core.config import settings
from app.db.redis import close_redis, init_redis
from src.ai.mcp_client.client import close_session

logger = logging.getLogger(__name__)


# ── Phase 14: startup recovery for failed embedding rows ──────────────────


async def _recover_pending_embeddings() -> None:
    """On startup, re-queue any embedding rows that failed to generate.

    The embedder writes a row with embedding_model="pending_retry" whenever
    the OpenAI call fails after all retries. This function finds those rows
    and schedules generate_embeddings() as a background asyncio task so they
    are retried once the app is running again.

    Failures here are logged but never raised — the app must start cleanly
    regardless of whether the embeddings table is reachable.
    """
    try:
        from sqlmodel import select

        from app.db.session import AsyncSessionLocal
        from app.models.embedding import Embedding
        from src.ai.utils.embeddings import generate_embeddings

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Embedding).where(Embedding.embedding_model == "pending_retry")
            )
            pending = result.scalars().all()

        if pending:
            logger.info(
                "[EMBEDDING RECOVERY] Re-queuing %d pending embedding(s)", len(pending)
            )
            for row in pending:
                asyncio.create_task(generate_embeddings(row.itinerary_id))

    except Exception as exc:
        logger.warning("[EMBEDDING RECOVERY] Could not check pending rows: %s", exc)


# ── Lifespan ───────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()
    await _recover_pending_embeddings()   # Phase 14
    yield
    await close_redis()
    await close_session()


# ── App ────────────────────────────────────────────────────────────────────


app = FastAPI(
    title="AI Trip Planner",
    description="Multi-agent AI travel planner — flights, hotels, activities & itineraries.",
    version="0.5.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.APP_ENV == "development" else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(trips_router)
app.include_router(users_router)   # Phase 16

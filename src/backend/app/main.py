"""
FastAPI application entry point.

  uvicorn app.main:app --reload --app-dir src/backend    (local dev, from the repo root)
  docker compose up backend                              (Docker)
"""

import logging
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlmodel import select, update
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes.admin import router as admin_router
from app.api.routes.auth import router as auth_router
from app.api.routes.health import router as health_router
from app.api.routes.trips import router as trips_router
from app.api.routes.users import router as users_router
from app.core.config import settings
from app.db.redis import close_redis, init_redis
from app.db.session import AsyncSessionLocal
from app.models.embedding import Embedding
from app.models.trip import Trip, TripStatus
from src.ai.embeddings.embedder import PENDING_RETRY_MODEL
from src.ai.mcp_client.client import close_session
from src.ai.utils.embeddings import EMBEDDINGS_PENDING_KEY, generate_embeddings
from src.ai.utils.tasks import spawn

logger = logging.getLogger(__name__)


# ── Startup recovery ───────────────────────────────────────────────────────


async def _recover_after_restart() -> None:
    """Repair what an unclean shutdown leaves behind.

    Planning runs and embedding jobs are in-process tasks, so none survives a
    restart:
      • a trip still "planning" was interrupted → mark it failed (Phase 13), so
        it can be planned again instead of answering 409 forever;
      • the in-flight embedding counter is stale → reset it;
      • embeddings that fell back to "pending_retry" → generate them again (Phase 14).

    Failures are logged, never raised — the app must boot even if the database
    is unreachable (GET /ping reports that in its body).
    """
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(update(Trip).where(Trip.status == TripStatus.PLANNING).values(status=TripStatus.FAILED))
            await db.commit()
            pending = (
                (
                    await db.execute(
                        select(Embedding.itinerary_id)
                        .where(Embedding.embedding_model == PENDING_RETRY_MODEL)
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )

        if pending:
            logger.info("[EMBEDDING RECOVERY] Re-queuing %d pending embedding(s)", len(pending))
        for itinerary_id in pending:
            spawn(generate_embeddings(itinerary_id))
    except Exception as exc:
        logger.warning("[STARTUP RECOVERY] skipped: %s", exc)


# ── Lifespan ───────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = await init_redis()
    try:
        await redis.delete(EMBEDDINGS_PENDING_KEY)
    except Exception as exc:
        logger.warning("Redis unavailable at startup: %s", exc)
    await _recover_after_restart()
    yield
    await close_redis()
    await close_session()


# ── App ────────────────────────────────────────────────────────────────────


app = FastAPI(
    title="AI Trip Planner",
    description="Multi-agent AI travel planner — flights, hotels, activities & itineraries.",
    version="0.17.0",
    lifespan=lifespan,
)

# Auth is a Bearer token (header, or ?token= for EventSource) — no cookies, so no
# credentialed CORS. Origins come from CORS_ORIGINS; production lists the real
# frontend origin(s) instead of a wildcard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Error envelope (Phase 17) ──────────────────────────────────────────────
# Every error body carries {"error": {"code", "message"}} for the frontend, next
# to FastAPI's own "detail" so existing clients keep working.


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={
            "detail": exc.detail,
            "error": {"code": HTTPStatus(exc.status_code).name, "message": str(exc.detail)},
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    message = "; ".join(f"{'.'.join(str(part) for part in err['loc'][1:])}: {err['msg']}" for err in exc.errors())
    return JSONResponse(
        status_code=422,
        content={
            "detail": jsonable_encoder(exc.errors()),
            "error": {"code": "VALIDATION_ERROR", "message": message},
        },
    )


app.include_router(health_router)
app.include_router(auth_router)
app.include_router(trips_router)
app.include_router(users_router)  # Phase 16
app.include_router(admin_router)  # Phase 14

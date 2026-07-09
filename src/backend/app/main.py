"""
FastAPI application entry point.

Start with:
    uvicorn app.main:app --reload          (local)
    docker compose up backend              (Docker)
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.health import router as health_router
from app.core.config import settings
from app.db.redis import close_redis, init_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # ── Startup ───────────────────────────────────────────────
    await init_redis()

    yield  # app is running

    # ── Shutdown ──────────────────────────────────────────────
    await close_redis()


app = FastAPI(
    title="AI Trip Planner",
    description=(
        "Multi-agent AI travel planner — flights, hotels, activities & itineraries."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS (dev: allow all, prod: restrict to Vercel domain) ───
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.APP_ENV == "development" else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────
app.include_router(health_router)

# Future routers registered here as phases complete:
# from app.api.routes.trips import router as trips_router   # Phase 5
# from app.api.routes.auth  import router as auth_router    # Phase 5

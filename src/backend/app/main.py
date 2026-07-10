"""
FastAPI application entry point.

  uvicorn app.main:app --reload     (local)
  docker compose up backend         (Docker)
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.health import router as health_router
from app.core.config import settings
from app.db.redis import close_redis, init_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()
    yield
    await close_redis()


app = FastAPI(
    title="AI Trip Planner",
    description="Multi-agent AI travel planner — flights, hotels, activities & itineraries.",
    version="0.1.0",
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

# Phase 5 — uncomment as routes are added:
# from app.api.routes.trips import router as trips_router
# from app.api.routes.auth  import router as auth_router

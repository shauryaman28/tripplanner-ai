"""
Shared fixtures for integration tests.

Requires Docker Postgres running. Skipped in CI unless RUN_INTEGRATION=1.

Usage in integration test files:

    async def test_can_create_user(db_session: AsyncSession):
        user = User(email="x@example.com", hashed_password="x")
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        assert user.id is not None
"""

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.config import settings

# Must import all models so SQLModel.metadata is fully populated before
# create_all / drop_all runs — same pattern as migrations/env.py.
from app.models import AgentRun, Embedding, Itinerary, Trip, User  # noqa: F401

_async_url = settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest_asyncio.fixture(scope="function")
async def db_session() -> AsyncSession:
    """
    Yields a live AsyncSession backed by a fresh schema.

    Lifecycle per test:
      1. Enable pgvector extension (idempotent — safe to run repeatedly).
      2. Create all tables via SQLModel.metadata (matches production schema).
      3. Yield the session — test runs here.
      4. Drop all tables — zero cross-test pollution.
    """
    engine = create_async_engine(_async_url, echo=False, pool_pre_ping=True)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)

    await engine.dispose()

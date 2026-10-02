"""Shared fixtures for integration tests (real Postgres + Redis from Docker).

Skipped unless RUN_INTEGRATION=1. tests/conftest.py has already pointed
`settings.DATABASE_URL` at the dedicated `<db>_test` database, so nothing here
can touch dev data.
"""

import pytest
import pytest_asyncio
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from tests.database import create_and_migrate


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    """Create the test database once and bring it to head with Alembic."""
    assert make_url(settings.DATABASE_URL).database.endswith("_test"), (
        "integration tests must never run against the dev database"
    )
    create_and_migrate()


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """Yield a live AsyncSession; every table is emptied after the test."""
    from app.db.session import engine as app_engine

    engine = create_async_engine(
        settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool
    )
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session

    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users CASCADE"))  # every other table hangs off users
    await engine.dispose()
    await app_engine.dispose()  # each test has its own event loop; don't leak pooled connections across them

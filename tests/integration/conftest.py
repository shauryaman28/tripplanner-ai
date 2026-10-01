"""Shared fixtures for integration tests (real Postgres + Redis from Docker).

Skipped unless RUN_INTEGRATION=1. tests/conftest.py has already pointed
`settings.DATABASE_URL` at the dedicated `<db>_test` database, so nothing here
can touch dev data.
"""

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    """Create the test database once and bring it to head with Alembic.

    Using the real migrations (not metadata.create_all) means the tests run
    against the production schema — cascades, HNSW index and all.
    """
    url = make_url(settings.DATABASE_URL).set(drivername="postgresql+psycopg2")
    assert url.database.endswith("_test"), "integration tests must never run against the dev database"

    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}):
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()

    command.upgrade(Config("alembic.ini"), "head")


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

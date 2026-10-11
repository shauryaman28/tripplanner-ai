"""Async SQLAlchemy session factory."""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.url import async_engine_arguments


def make_engine(database_url: str) -> AsyncEngine:
    """The app's engine for a database URL as it is handed out.

    Phase 27: app/db/url.py turns libpq's parameters into asyncpg's and keeps no
    prepared statement, so that a connection pooler can sit in between.
    """
    url, connect_args = async_engine_arguments(database_url)
    return create_async_engine(
        url,
        echo=settings.APP_ENV == "development",
        connect_args=connect_args,
        # A serverless Postgres sleeps when nobody asks it anything, and its connections die with it:
        # one is tried before it is used, and none is kept past the five minutes after which it may be gone.
        pool_pre_ping=True,
        pool_recycle=300,
    )


engine = make_engine(settings.DATABASE_URL)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a DB session, always closes it."""
    async with AsyncSessionLocal() as session:
        yield session


async def get_db_raw() -> None:
    """Lightweight connectivity check used by /ping."""
    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT 1"))

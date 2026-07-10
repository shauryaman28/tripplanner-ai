"""Async SQLAlchemy session factory."""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

_async_url = settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(
    _async_url,
    echo=settings.APP_ENV == "development",
    pool_pre_ping=True,
)

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

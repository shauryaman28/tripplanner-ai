"""
Async database session factory.

Usage in FastAPI endpoints:
    async def route(db: AsyncSession = Depends(get_db)): ...

Usage in tests:
    See tests/unit/conftest.py for the test DB fixture.
"""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# Convert postgresql:// → postgresql+asyncpg:// for the async driver
_async_url = settings.DATABASE_URL.replace(
    "postgresql://", "postgresql+asyncpg://", 1
)

engine = create_async_engine(
    _async_url,
    echo=settings.APP_ENV == "development",   # SQL logging in dev only
    pool_pre_ping=True,                        # recycles stale connections
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
    """
    Lightweight connectivity check — executes SELECT 1.
    Raises on any connection failure.
    Used by /ping so tests can mock this single function cleanly.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT 1"))

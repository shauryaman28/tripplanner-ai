"""
Shared pytest fixtures for unit tests.

These fixtures use mocks/stubs — they never touch Docker or real services.
Tests that need real Postgres/Redis live in tests/integration/.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def mock_redis():
    """
    Returns a mock Redis client.
    ping() returns True, get/set/delete return sensible defaults.
    """
    r = AsyncMock()
    r.ping.return_value = True
    r.get.return_value = None
    r.set.return_value = True
    r.delete.return_value = 1
    return r


@pytest.fixture
def mock_db_session():
    """Returns a mock AsyncSession."""
    session = AsyncMock()
    session.execute.return_value = MagicMock()
    session.commit.return_value = None
    session.rollback.return_value = None
    return session


@pytest_asyncio.fixture
async def test_client(mock_redis):
    """
    AsyncClient for the FastAPI app with Redis mocked out.
    Postgres connectivity is also mocked so tests run without Docker.
    """
    from app.main import app
    from app.db import redis as redis_module
    from app.db.session import AsyncSessionLocal
    from sqlalchemy.ext.asyncio import AsyncSession
    from unittest.mock import patch, AsyncMock
    from sqlalchemy import text

    # Patch Redis singleton
    with patch.object(redis_module, "redis_client", mock_redis):
        # Patch DB session to avoid needing real Postgres
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock()

        with patch.object(AsyncSessionLocal, "__call__", return_value=mock_session):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield client

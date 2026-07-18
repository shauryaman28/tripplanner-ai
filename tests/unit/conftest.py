"""Shared pytest fixtures for unit tests. No Docker or real services."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.ping.return_value = True
    r.get.return_value = None
    r.set.return_value = True
    r.delete.return_value = 1
    r.publish = AsyncMock(return_value=1)
    return r


@pytest.fixture
def mock_db_session():
    session = AsyncMock()
    session.execute.return_value = MagicMock()
    session.commit.return_value = None
    session.rollback.return_value = None
    return session


@pytest_asyncio.fixture
async def test_client(mock_redis):
    """
    Full test client with mocked Postgres and Redis.
    Suitable for health, auth, and trip route unit tests.
    """
    from app.db import redis as redis_module
    from app.db.session import AsyncSessionLocal
    from app.main import app
    from sqlalchemy.ext.asyncio import AsyncSession

    with patch.object(redis_module, "redis_client", mock_redis):
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock()
        mock_session.get = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        with patch.object(AsyncSessionLocal, "__call__", return_value=mock_session):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield client

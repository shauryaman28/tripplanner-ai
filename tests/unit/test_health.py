"""
Unit tests for GET /ping

Mocking strategy:
  - app.api.routes.health.get_db_raw  →  patched to succeed or raise
  - app.api.routes.health.get_redis   →  patched to return mock redis

Tests run without Docker. Phase 1 done criterion: all 4 pass.
"""

from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_ping_returns_200_with_ok_status():
    """Happy path: both services healthy → 200 {"postgres":"ok","redis":"ok"}."""
    from app.main import app

    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True

    async def mock_get_db_raw():
        return None

    async def mock_get_redis():
        return mock_redis

    with patch("app.api.routes.health.get_db_raw", mock_get_db_raw):
        with patch("app.api.routes.health.get_redis", mock_get_redis):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/ping")

    assert response.status_code == 200
    assert response.json() == {"postgres": "ok", "redis": "ok"}


@pytest.mark.asyncio
async def test_ping_returns_503_when_postgres_down():
    """Postgres unavailable → 503 with 'Postgres' in detail."""
    from app.main import app

    async def mock_get_db_raw_fail():
        raise ConnectionError("could not connect to server")

    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True

    async def mock_get_redis():
        return mock_redis

    with patch("app.api.routes.health.get_db_raw", mock_get_db_raw_fail):
        with patch("app.api.routes.health.get_redis", mock_get_redis):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/ping")

    assert response.status_code == 503
    assert "Postgres" in response.json()["detail"]


@pytest.mark.asyncio
async def test_ping_returns_503_when_redis_down():
    """Redis unavailable → 503 with 'Redis' in detail."""
    from app.main import app

    async def mock_get_db_raw():
        return None

    mock_redis = AsyncMock()
    mock_redis.ping.side_effect = ConnectionError("Redis connection refused")

    async def mock_get_redis():
        return mock_redis

    with patch("app.api.routes.health.get_db_raw", mock_get_db_raw):
        with patch("app.api.routes.health.get_redis", mock_get_redis):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/ping")

    assert response.status_code == 503
    assert "Redis" in response.json()["detail"]


@pytest.mark.asyncio
async def test_ping_response_shape_is_exact():
    """Success response has exactly the keys 'postgres' and 'redis', no extras."""
    from app.main import app

    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True

    async def mock_get_db_raw():
        return None

    async def mock_get_redis():
        return mock_redis

    with patch("app.api.routes.health.get_db_raw", mock_get_db_raw):
        with patch("app.api.routes.health.get_redis", mock_get_redis):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/ping")

    assert set(response.json().keys()) == {"postgres", "redis"}

"""
Integration test for /ping — requires real Docker services.

Run with:
    RUN_INTEGRATION=1 pytest tests/integration/

Skipped automatically in CI unless RUN_INTEGRATION env var is set.
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)


@pytest.mark.asyncio
async def test_ping_against_real_services():
    from app.db.redis import close_redis, init_redis
    from app.main import app

    await init_redis()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/ping")
    finally:
        await close_redis()

    assert response.status_code == 200
    body = response.json()
    assert body["postgres"] == "ok"
    assert body["redis"] == "ok"

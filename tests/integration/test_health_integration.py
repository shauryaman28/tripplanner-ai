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
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ping")

    assert response.status_code == 200
    body = response.json()
    assert body["postgres"] == "ok"
    assert body["redis"] == "ok"

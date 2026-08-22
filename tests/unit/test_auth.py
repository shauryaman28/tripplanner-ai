"""Unit tests for /auth/register and /auth/login. No Docker needed.

Uses FastAPI's dependency_overrides to replace get_db entirely for the
duration of each test. This fully bypasses the real async engine (and
its event-loop binding) instead of trying to patch AsyncSessionLocal's
__call__ — Python looks up dunder methods on the *type* for implicit
invocation (`AsyncSessionLocal()`), so an instance-level patch on
__call__ is silently ignored and the real engine gets touched anyway.
That mismatch was the actual cause of the "attached to a different
loop" failures.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


def _make_mock_session(existing_user=None, created_user=None):
    """Return a configured AsyncMock session.

    No __aenter__/__aexit__ needed here — the dependency override below
    yields this object directly, exactly matching what Depends(get_db)
    would normally hand the route.
    """
    session = AsyncMock()

    scalar_mock = MagicMock()
    scalar_mock.scalar_one_or_none.return_value = existing_user
    session.execute = AsyncMock(return_value=scalar_mock)
    session.get = AsyncMock(return_value=existing_user)

    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _refresh(obj):
        if created_user:
            obj.id = created_user.id
            obj.email = created_user.email
            obj.created_at = created_user.created_at

    session.refresh = _refresh
    return session


def _override_get_db(mock_session):
    async def override():
        yield mock_session

    return override


@pytest.mark.asyncio
async def test_register_new_user_returns_201():
    from app.db.session import get_db
    from app.main import app

    fake_user = MagicMock()
    fake_user.id = uuid.uuid4()
    fake_user.email = "test@example.com"
    fake_user.created_at = datetime.now(timezone.utc)

    mock_session = _make_mock_session(existing_user=None, created_user=fake_user)

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/auth/register", json={"email": "test@example.com", "password": "pass123"})
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 201
    assert resp.json()["email"] == "test@example.com"


@pytest.mark.asyncio
async def test_register_duplicate_email_returns_400():
    from app.db.session import get_db
    from app.main import app

    existing = MagicMock()
    existing.email = "dupe@example.com"
    mock_session = _make_mock_session(existing_user=existing)

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/auth/register", json={"email": "dupe@example.com", "password": "pass123"})
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 400
    assert "already registered" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_login_invalid_credentials_returns_401():
    from app.db.session import get_db
    from app.main import app

    mock_session = _make_mock_session(existing_user=None)

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/auth/login",
                    data={"username": "nobody@example.com", "password": "wrong"},
                )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_valid_credentials_returns_token():
    from app.core.security import hash_password
    from app.db.session import get_db
    from app.main import app

    real_user = MagicMock()
    real_user.id = uuid.uuid4()
    real_user.email = "user@example.com"
    real_user.hashed_password = hash_password("correct_password")
    mock_session = _make_mock_session(existing_user=real_user)

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/auth/login",
                    data={"username": "user@example.com", "password": "correct_password"},
                )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_protected_route_without_token_returns_401():
    from app.main import app

    with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/trips",
                json={
                    "destination": "Goa",
                    "start_date": "2025-12-10",
                    "end_date": "2025-12-17",
                    "budget": 50000,
                },
            )

    assert resp.status_code == 401

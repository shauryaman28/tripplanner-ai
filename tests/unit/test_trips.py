"""Unit tests for trip routes. No Docker needed.

Same dependency_overrides fix as test_auth.py — see that file's module
docstring for why patching AsyncSessionLocal.__call__ was silently
ignored and caused these tests to hit the real engine/event loop.
"""

import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token


def _auth_header(user_id: uuid.UUID) -> dict:
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _make_trip(user_id: uuid.UUID) -> MagicMock:
    trip = MagicMock()
    trip.id = uuid.uuid4()
    trip.user_id = user_id
    trip.destination = "Goa"
    trip.start_date = date(2025, 12, 10)
    trip.end_date = date(2025, 12, 17)
    trip.budget = 50_000.0
    trip.group_size = 2
    trip.interests = ["beach", "food"]
    trip.status = "pending"
    trip.created_at = datetime.now(timezone.utc)
    return trip


def _make_user(user_id: uuid.UUID) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.email = "traveller@example.com"
    user.created_at = datetime.now(timezone.utc)
    return user


def _override_get_db(mock_session):
    async def override():
        yield mock_session
    return override


@pytest.mark.asyncio
async def test_create_trip_requires_auth():
    from app.main import app

    with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/trips", json={
                "destination": "Goa",
                "start_date": "2025-12-10",
                "end_date": "2025-12-17",
                "budget": 50000,
            })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_trip_returns_201():
    from app.db.session import get_db
    from app.main import app

    user_id = uuid.uuid4()
    fake_user = _make_user(user_id)
    fake_trip = _make_trip(user_id)

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=fake_user)
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    async def _refresh(obj):
        obj.id = fake_trip.id
        obj.user_id = fake_trip.user_id
        obj.destination = fake_trip.destination
        obj.start_date = fake_trip.start_date
        obj.end_date = fake_trip.end_date
        obj.budget = fake_trip.budget
        obj.group_size = fake_trip.group_size
        obj.interests = fake_trip.interests
        obj.status = fake_trip.status
        obj.created_at = fake_trip.created_at

    mock_session.refresh = _refresh

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/trips",
                    json={
                        "destination": "Goa",
                        "start_date": "2025-12-10",
                        "end_date": "2025-12-17",
                        "budget": 50000,
                        "group_size": 2,
                        "interests": ["beach", "food"],
                    },
                    headers=_auth_header(user_id),
                )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 201
    data = resp.json()
    assert data["destination"] == "Goa"
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_create_trip_end_date_before_start_returns_422():
    from app.db.session import get_db
    from app.main import app

    user_id = uuid.uuid4()
    fake_user = _make_user(user_id)

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=fake_user)

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/trips",
                    json={
                        "destination": "Goa",
                        "start_date": "2025-12-17",
                        "end_date": "2025-12-10",   # end before start
                        "budget": 50000,
                    },
                    headers=_auth_header(user_id),
                )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_trips_returns_user_trips():
    from app.db.session import get_db
    from app.main import app

    user_id = uuid.uuid4()
    fake_user = _make_user(user_id)
    fake_trip = _make_trip(user_id)

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=fake_user)

    trips_result = MagicMock()
    trips_result.scalars.return_value.all.return_value = [fake_trip]
    mock_session.execute = AsyncMock(return_value=trips_result)

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/trips", headers=_auth_header(user_id))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["destination"] == "Goa"


@pytest.mark.asyncio
async def test_get_trip_runs_returns_empty_list():
    from app.db.session import get_db
    from app.main import app

    user_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    fake_user = _make_user(user_id)
    fake_trip = _make_trip(user_id)
    fake_trip.id = trip_id

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=fake_user)

    # execute #1 → _get_trip_or_404; execute #2 → AgentRun query
    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = fake_trip
    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(side_effect=[trip_result, runs_result])

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(
                    f"/trips/{trip_id}/runs",
                    headers=_auth_header(user_id),
                )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_similar_returns_501():
    from app.db.session import get_db
    from app.main import app

    user_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    fake_user = _make_user(user_id)
    fake_trip = _make_trip(user_id)
    fake_trip.id = trip_id

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=fake_user)

    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = fake_trip
    mock_session.execute = AsyncMock(return_value=trip_result)

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(
                    f"/trips/{trip_id}/similar",
                    headers=_auth_header(user_id),
                )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 501
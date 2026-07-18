"""Unit tests for trip routes. No Docker needed."""

import uuid
from datetime import date, datetime
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
    trip.created_at = datetime.utcnow()
    return trip


def _make_user(user_id: uuid.UUID) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.email = "traveller@example.com"
    user.created_at = datetime.utcnow()
    return user


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
    from app.db.session import AsyncSessionLocal, get_db
    from app.main import app

    user_id = uuid.uuid4()
    fake_user = _make_user(user_id)
    fake_trip = _make_trip(user_id)

    from sqlalchemy.ext.asyncio import AsyncSession

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    # get() for JWT user lookup
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

    with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))), \
         patch.object(AsyncSessionLocal, "__call__", return_value=mock_session):
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

    assert resp.status_code == 201
    data = resp.json()
    assert data["destination"] == "Goa"
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_create_trip_end_date_before_start_returns_422():
    from app.db.session import AsyncSessionLocal
    from app.main import app

    user_id = uuid.uuid4()
    fake_user = _make_user(user_id)

    from sqlalchemy.ext.asyncio import AsyncSession

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = AsyncMock(return_value=fake_user)

    with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))), \
         patch.object(AsyncSessionLocal, "__call__", return_value=mock_session):
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

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_trip_runs_returns_empty_list():
    from app.db.session import AsyncSessionLocal
    from app.main import app

    user_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    fake_user = _make_user(user_id)
    fake_trip = _make_trip(user_id)
    fake_trip.id = trip_id

    from sqlalchemy.ext.asyncio import AsyncSession

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = AsyncMock(return_value=fake_user)

    # First execute → trip lookup; second execute → agent_runs (empty)
    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = fake_trip
    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(side_effect=[trip_result, trip_result, runs_result])

    with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))), \
         patch.object(AsyncSessionLocal, "__call__", return_value=mock_session):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                f"/trips/{trip_id}/runs",
                headers=_auth_header(user_id),
            )

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_similar_returns_501():
    from app.db.session import AsyncSessionLocal
    from app.main import app

    user_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    fake_user = _make_user(user_id)
    fake_trip = _make_trip(user_id)
    fake_trip.id = trip_id

    from sqlalchemy.ext.asyncio import AsyncSession

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = AsyncMock(return_value=fake_user)

    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = fake_trip
    mock_session.execute = AsyncMock(return_value=trip_result)

    with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))), \
         patch.object(AsyncSessionLocal, "__call__", return_value=mock_session):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                f"/trips/{trip_id}/similar",
                headers=_auth_header(user_id),
            )

    assert resp.status_code == 501

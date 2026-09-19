"""
Unit tests for Phase 17 backend additions.

The only new backend code is GET /trips/{id}/status.
All tests follow the dependency_overrides pattern from test_trips.py.
No Docker required.
"""

import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token
from app.models.agent_run import AgentRun
from app.models.trip import Trip, TripStatus


def _auth(user_id: uuid.UUID) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def _make_user(uid: uuid.UUID) -> MagicMock:
    u = MagicMock()
    u.id = uid
    u.email = "t@test.com"
    u.created_at = datetime.now(timezone.utc)
    return u


def _make_trip(uid: uuid.UUID, tid: uuid.UUID, status: str = "planning") -> MagicMock:
    t = MagicMock(spec=Trip)
    t.id = tid
    t.user_id = uid
    t.destination = "Goa"
    t.start_date = date(2026, 12, 10)
    t.end_date   = date(2026, 12, 17)
    t.budget     = 50_000.0
    t.group_size = 2
    t.interests  = ["beach"]
    t.status     = status
    t.created_at = datetime.now(timezone.utc)
    return t


def _make_run(trip_id: uuid.UUID, agent_name: str, status: str) -> AgentRun:
    return AgentRun(
        id=uuid.uuid4(),
        trip_id=trip_id,
        agent_name=agent_name,
        status=status,
        input={},
        output={},
        duration_ms=100,
        turn=1,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def _override_get_db(session):
    async def _dep():
        yield session
    return _dep


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_trip_status_returns_correct_shape():
    """GET /trips/{id}/status returns status + progress object."""
    from app.db.session import get_db
    from app.main import app

    uid, tid = uuid.uuid4(), uuid.uuid4()
    fake_user = _make_user(uid)
    fake_trip = _make_trip(uid, tid, "planning")

    flight_run     = _make_run(tid, "flight_agent",     "completed")
    hotel_run      = _make_run(tid, "hotel_agent",      "running")
    activities_run = _make_run(tid, "activities_agent", "pending")

    session = AsyncMock()
    session.get = AsyncMock(return_value=fake_user)

    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = fake_trip

    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = [flight_run, hotel_run, activities_run]

    session.execute = AsyncMock(side_effect=[trip_result, runs_result])

    app.dependency_overrides[get_db] = _override_get_db(session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/trips/{tid}/status", headers=_auth(uid))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "planning"
    assert data["trip_id"] == str(tid)
    assert "progress" in data
    assert data["progress"]["agents_total"] == 3
    assert data["progress"]["agents"]["flight_agent"] == "completed"
    assert data["progress"]["agents"]["hotel_agent"] == "running"


@pytest.mark.asyncio
async def test_get_trip_status_agents_done_count():
    """agents_done reflects only completed agents."""
    from app.db.session import get_db
    from app.main import app

    uid, tid = uuid.uuid4(), uuid.uuid4()
    fake_user = _make_user(uid)
    fake_trip = _make_trip(uid, tid, "planning")

    runs = [
        _make_run(tid, "flight_agent",     "completed"),
        _make_run(tid, "hotel_agent",      "completed"),
        _make_run(tid, "activities_agent", "failed"),
    ]

    session = AsyncMock()
    session.get = AsyncMock(return_value=fake_user)

    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = fake_trip
    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = runs
    session.execute = AsyncMock(side_effect=[trip_result, runs_result])

    app.dependency_overrides[get_db] = _override_get_db(session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/trips/{tid}/status", headers=_auth(uid))
    finally:
        app.dependency_overrides.pop(get_db, None)

    data = resp.json()
    assert data["progress"]["agents_done"] == 2   # only completed ones


@pytest.mark.asyncio
async def test_get_trip_status_no_runs_returns_all_pending():
    """When no agent_runs exist yet, all agents report 'pending'."""
    from app.db.session import get_db
    from app.main import app

    uid, tid = uuid.uuid4(), uuid.uuid4()
    fake_user = _make_user(uid)
    fake_trip = _make_trip(uid, tid, "pending")

    session = AsyncMock()
    session.get = AsyncMock(return_value=fake_user)

    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = fake_trip
    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(side_effect=[trip_result, runs_result])

    app.dependency_overrides[get_db] = _override_get_db(session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/trips/{tid}/status", headers=_auth(uid))
    finally:
        app.dependency_overrides.pop(get_db, None)

    data = resp.json()
    assert data["progress"]["agents_done"] == 0
    assert all(v == "pending" for v in data["progress"]["agents"].values())


@pytest.mark.asyncio
async def test_get_trip_status_requires_auth():
    from app.main import app
    with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/trips/{uuid.uuid4()}/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_trip_status_404_for_wrong_user():
    from app.db.session import get_db
    from app.main import app

    uid, tid = uuid.uuid4(), uuid.uuid4()
    fake_user = _make_user(uid)

    session = AsyncMock()
    session.get = AsyncMock(return_value=fake_user)
    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = None   # not found for this user
    session.execute = AsyncMock(return_value=trip_result)

    app.dependency_overrides[get_db] = _override_get_db(session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/trips/{tid}/status", headers=_auth(uid))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_trip_status_completed_trip():
    """A completed trip returns status=completed and agents_done=3."""
    from app.db.session import get_db
    from app.main import app

    uid, tid = uuid.uuid4(), uuid.uuid4()
    fake_user = _make_user(uid)
    fake_trip = _make_trip(uid, tid, "completed")

    runs = [
        _make_run(tid, "flight_agent",     "completed"),
        _make_run(tid, "hotel_agent",      "completed"),
        _make_run(tid, "activities_agent", "completed"),
    ]

    session = AsyncMock()
    session.get = AsyncMock(return_value=fake_user)

    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = fake_trip
    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = runs
    session.execute = AsyncMock(side_effect=[trip_result, runs_result])

    app.dependency_overrides[get_db] = _override_get_db(session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(f"/trips/{tid}/status", headers=_auth(uid))
    finally:
        app.dependency_overrides.pop(get_db, None)

    data = resp.json()
    assert data["status"] == "completed"
    assert data["progress"]["agents_done"] == 3

"""
Unit tests for clarification API endpoints (Phase 7B & Phase 9 Orchestrator).

All tests mock OrchestratorAgent.run() and the conversation utilities so no
MCP server, no Redis, and no Postgres are needed.
"""

import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token

# ── Helpers ────────────────────────────────────────────────────────────────


def _auth(user_id: uuid.UUID) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def _make_user(uid: uuid.UUID) -> MagicMock:
    u = MagicMock()
    u.id = uid
    u.email = "t@test.com"
    u.created_at = datetime.now(timezone.utc)
    return u


def _make_trip(uid: uuid.UUID, tid: uuid.UUID) -> MagicMock:
    t = MagicMock()
    t.id = tid
    t.user_id = uid
    t.destination = "Goa"
    t.start_date = date(2026, 12, 10)
    t.end_date = date(2026, 12, 17)
    t.budget = 50_000.0
    t.group_size = 2
    t.status = "pending"
    t.created_at = datetime.now(timezone.utc)
    return t


def _session_with_trip(trip: MagicMock) -> AsyncMock:
    s = AsyncMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = trip
    s.execute = AsyncMock(return_value=res)
    s.get = AsyncMock(return_value=_make_user(trip.user_id))
    s.add = MagicMock()
    s.commit = AsyncMock()
    return s


def _override_get_db(session):
    async def _dep():
        yield session

    return _dep


# ── POST /trips/{id}/plan ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_with_structured_data_returns_planning_started():
    """No raw_input → structured fields → planning_started."""
    from app.db.session import get_db
    from app.main import app

    uid, tid = uuid.uuid4(), uuid.uuid4()
    mock_session = _session_with_trip(_make_trip(uid, tid))

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True), publish=AsyncMock())):
            with patch("app.api.routes.trips.OrchestratorAgent") as MockOA:
                MockOA.return_value.run = AsyncMock(return_value={})
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    resp = await c.post(f"/trips/{tid}/plan", headers=_auth(uid))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 202
    assert resp.json()["status"] == "planning_started"


@pytest.mark.asyncio
async def test_plan_with_raw_input_returns_planning_started():
    """raw_input passed → starts orchestrator planning."""
    from app.db.session import get_db
    from app.main import app

    uid, tid = uuid.uuid4(), uuid.uuid4()
    mock_session = _session_with_trip(_make_trip(uid, tid))

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch(
            "app.db.redis.redis_client",
            AsyncMock(
                ping=AsyncMock(return_value=True),
                publish=AsyncMock(),
            ),
        ):
            with patch("app.api.routes.trips.OrchestratorAgent") as MockOA:
                MockOA.return_value.run = AsyncMock(return_value={})
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    resp = await c.post(
                        f"/trips/{tid}/plan",
                        json={"raw_input": "Plan a trip to Goa in Dec"},
                        headers=_auth(uid),
                    )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 202
    assert resp.json()["status"] == "planning_started"


# ── POST /trips/{id}/clarify ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clarify_answer_starts_planning():
    """User answers the question → planning_started."""
    from app.db.session import get_db
    from app.main import app

    uid, tid = uuid.uuid4(), uuid.uuid4()
    mock_session = _session_with_trip(_make_trip(uid, tid))

    saved_state = {
        "destination": "Goa",
        "start_date": None,
        "budget": None,
    }

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch(
            "app.db.redis.redis_client",
            AsyncMock(
                ping=AsyncMock(return_value=True),
                publish=AsyncMock(),
            ),
        ):
            with patch("app.api.routes.trips.get_trip_state", AsyncMock(return_value=saved_state)):
                with patch("app.api.routes.trips.append_history", AsyncMock(return_value=[])):
                    with patch("app.api.routes.trips.OrchestratorAgent") as MockOA:
                        MockOA.return_value.run = AsyncMock(return_value={})
                        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                            resp = await c.post(
                                f"/trips/{tid}/clarify",
                                json={"answer": "December 15"},
                                headers=_auth(uid),
                            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert resp.json()["status"] == "planning_started"


@pytest.mark.asyncio
async def test_clarify_requires_auth():
    from app.main import app

    with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(f"/trips/{uuid.uuid4()}/clarify", json={"answer": "Goa"})

    assert resp.status_code == 401

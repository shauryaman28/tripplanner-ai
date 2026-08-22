"""
Unit tests for Phase 7B — clarification API flow.

All tests mock FlightAgent.run() and the conversation utilities so no
MCP server, no Redis, and no Postgres are needed.
"""

import json
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
    """No raw_input → structured fields → router says search → planning_started."""
    from app.db.session import get_db
    from app.main import app

    uid, tid = uuid.uuid4(), uuid.uuid4()
    mock_session = _session_with_trip(_make_trip(uid, tid))

    agent_result = {"flights": [{"airline": "6E"}], "error": None, "clarification_question": None}

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True), publish=AsyncMock())):
            with patch("app.api.routes.trips.FlightAgent") as MockFA:
                MockFA.return_value.run = AsyncMock(return_value=agent_result)
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    resp = await c.post(f"/trips/{tid}/plan", headers=_auth(uid))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 202
    assert resp.json()["status"] == "planning_started"


@pytest.mark.asyncio
async def test_plan_ambiguous_raw_input_returns_clarification():
    """raw_input that the agent can't resolve → clarification_needed response."""
    from app.db.session import get_db
    from app.main import app

    uid, tid = uuid.uuid4(), uuid.uuid4()
    mock_session = _session_with_trip(_make_trip(uid, tid))

    agent_result = {
        "flights": [],
        "error": None,
        "clarification_question": "What date would you like to travel?",
    }

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(
            ping=AsyncMock(return_value=True),
            publish=AsyncMock(),
            get=AsyncMock(return_value=None),
            setex=AsyncMock(),
        )):
            with patch("app.api.routes.trips.save_trip_state", AsyncMock()):
                with patch("app.api.routes.trips.append_history", AsyncMock(return_value=[])):
                    with patch("app.api.routes.trips.FlightAgent") as MockFA:
                        MockFA.return_value.run = AsyncMock(return_value=agent_result)
                        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                            resp = await c.post(
                                f"/trips/{tid}/plan",
                                json={"raw_input": "I want to go somewhere warm"},
                                headers=_auth(uid),
                            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "clarification_needed"
    assert body["question"] == "What date would you like to travel?"
    assert body["trip_id"] == str(tid)


# ── POST /trips/{id}/clarify ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clarify_answer_completes_planning():
    """User answers the question → agent succeeds → planning_started."""
    from app.db.session import get_db
    from app.main import app

    uid, tid = uuid.uuid4(), uuid.uuid4()
    mock_session = _session_with_trip(_make_trip(uid, tid))

    # State stored by the previous /plan call
    saved_state = {
        "destination": "GOI",
        "date": None,
        "budget": None,
        "clarification_question": "What date would you like to travel?",
        "flights": [],
        "error": None,
    }
    agent_result = {"flights": [{"airline": "6E"}], "error": None, "clarification_question": None}

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(
            ping=AsyncMock(return_value=True),
            publish=AsyncMock(),
        )):
            with patch("app.api.routes.trips.get_trip_state", AsyncMock(return_value=saved_state)):
                with patch("app.api.routes.trips.append_history", AsyncMock(return_value=[])):
                    with patch("app.api.routes.trips.FlightAgent") as MockFA:
                        MockFA.return_value.run = AsyncMock(return_value=agent_result)
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
async def test_clarify_still_missing_field_returns_next_question():
    """One answer given but another field still missing → another clarification."""
    from app.db.session import get_db
    from app.main import app

    uid, tid = uuid.uuid4(), uuid.uuid4()
    mock_session = _session_with_trip(_make_trip(uid, tid))

    saved_state = {
        "destination": "GOI",
        "date": None,
        "budget": None,
        "clarification_question": "What date would you like to travel?",
    }
    # Agent still can't proceed — now asking about budget
    agent_result = {
        "flights": [],
        "error": None,
        "clarification_question": "What is your approximate budget for flights in INR?",
    }

    app.dependency_overrides[get_db] = _override_get_db(mock_session)
    try:
        with patch("app.db.redis.redis_client", AsyncMock(
            ping=AsyncMock(return_value=True),
            publish=AsyncMock(),
        )):
            with patch("app.api.routes.trips.get_trip_state", AsyncMock(return_value=saved_state)):
                with patch("app.api.routes.trips.save_trip_state", AsyncMock()):
                    with patch("app.api.routes.trips.append_history", AsyncMock(return_value=[])):
                        with patch("app.api.routes.trips.FlightAgent") as MockFA:
                            MockFA.return_value.run = AsyncMock(return_value=agent_result)
                            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                                resp = await c.post(
                                    f"/trips/{tid}/clarify",
                                    json={"answer": "December 15"},
                                    headers=_auth(uid),
                                )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "clarification_needed"
    assert "budget" in body["question"].lower()


@pytest.mark.asyncio
async def test_clarify_requires_auth():
    from app.main import app

    with patch("app.db.redis.redis_client", AsyncMock(ping=AsyncMock(return_value=True))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(f"/trips/{uuid.uuid4()}/clarify", json={"answer": "Goa"})

    assert resp.status_code == 401

"""
Unit tests for Phase 15 — Multi-turn refinement.

Zero network calls. All LLM and DB interactions are mocked.

Dev A — RefinementClassifier:
  - classify_refinement returns correct type for hotel message
  - classify_refinement returns correct type for flight message
  - classify_refinement "make it cheaper" → targeted_flights (hard rule)
  - classify_refinement "add a day" → add_day (hard rule)
  - classify_refinement destination change → full_replan (hard rule)
  - classify_refinement falls back to full_replan on LLM failure

Dev B — Conversation history turn tracking:
  - append_history adds turn field to each entry
  - get_current_turn returns 0 on empty history
  - get_current_turn returns highest turn in history
  - append_history defaults turn=1 (backward compatible)

Dev C — run_logger turn propagation:
  - log_agent_run writes turn=2 correctly
  - get_retry_chain includes turn in each entry

Dev D — API endpoints:
  - GET /trips/{id}/runs?turn=1 filters correctly
  - GET /trips/{id}/runs returns all turns when no filter
  - POST /trips/{id}/refine returns 409 when no prior state
  - POST /trips/{id}/refine returns refinement_started with correct turn
  - GET /trips/{id}/itineraries returns all versions newest-first
  - _run_orchestrator saves planning state to Redis after completion
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token
from src.ai.agents.refinement_classifier import (
    RefinementClassification,
    classify_refinement,
)
from src.ai.utils.conversation import append_history, get_current_turn, get_history


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_redis() -> AsyncMock:
    """In-memory fake Redis backed by a plain dict."""
    store: dict = {}
    r = AsyncMock()
    r.get = AsyncMock(side_effect=lambda k: store.get(k))
    r.setex = AsyncMock(side_effect=lambda k, _ttl, v: store.update({k: v}))
    r._store = store
    return r


# ── Dev A — RefinementClassifier ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_hotel_message():
    mock_response = MagicMock()
    mock_response.content = '{"refinement_type": "targeted_hotel", "reason": "User wants different hotel."}'

    with patch("src.ai.agents.refinement_classifier.ChatGoogleGenerativeAI") as MockLLM:
        MockLLM.return_value.ainvoke = AsyncMock(return_value=mock_response)
        result = await classify_refinement("Switch to a beachfront hotel", [])

    assert result.refinement_type == "targeted_hotel"
    assert isinstance(result.reason, str)


@pytest.mark.asyncio
async def test_classify_flight_message():
    mock_response = MagicMock()
    mock_response.content = '{"refinement_type": "targeted_flights", "reason": "User wants direct flight."}'

    with patch("src.ai.agents.refinement_classifier.ChatGoogleGenerativeAI") as MockLLM:
        MockLLM.return_value.ainvoke = AsyncMock(return_value=mock_response)
        result = await classify_refinement("Find me a non-stop flight", [])

    assert result.refinement_type == "targeted_flights"


@pytest.mark.asyncio
async def test_classify_make_it_cheaper_is_targeted_flights():
    """Hard rule: 'make it cheaper' → targeted_flights regardless of phrasing."""
    mock_response = MagicMock()
    mock_response.content = '{"refinement_type": "targeted_flights", "reason": "Cheapest lever is flights."}'

    with patch("src.ai.agents.refinement_classifier.ChatGoogleGenerativeAI") as MockLLM:
        MockLLM.return_value.ainvoke = AsyncMock(return_value=mock_response)
        result = await classify_refinement("Make it cheaper", [])

    assert result.refinement_type == "targeted_flights"


@pytest.mark.asyncio
async def test_classify_add_a_day_is_add_day():
    mock_response = MagicMock()
    mock_response.content = '{"refinement_type": "add_day", "reason": "Trip extension."}'

    with patch("src.ai.agents.refinement_classifier.ChatGoogleGenerativeAI") as MockLLM:
        MockLLM.return_value.ainvoke = AsyncMock(return_value=mock_response)
        result = await classify_refinement("Add a day to the trip", [])

    assert result.refinement_type == "add_day"


@pytest.mark.asyncio
async def test_classify_destination_change_is_full_replan():
    mock_response = MagicMock()
    mock_response.content = '{"refinement_type": "full_replan", "reason": "Destination changed."}'

    with patch("src.ai.agents.refinement_classifier.ChatGoogleGenerativeAI") as MockLLM:
        MockLLM.return_value.ainvoke = AsyncMock(return_value=mock_response)
        result = await classify_refinement("I'd rather go to Manali", [])

    assert result.refinement_type == "full_replan"


@pytest.mark.asyncio
async def test_classify_llm_failure_falls_back_to_full_replan():
    """On any exception, classifier must return full_replan — the safe default."""
    with patch("src.ai.agents.refinement_classifier.ChatGoogleGenerativeAI") as MockLLM:
        MockLLM.return_value.ainvoke = AsyncMock(side_effect=Exception("API error"))
        result = await classify_refinement("Something weird", [])

    assert result.refinement_type == "full_replan"
    assert "Classification failed" in result.reason


# ── Dev B — conversation history turn tracking ────────────────────────────────


@pytest.mark.asyncio
async def test_append_history_adds_turn_field():
    r = _make_redis()
    trip_id = str(uuid.uuid4())
    await append_history(r, trip_id, role="user", content="Hello", turn=2)
    hist = await get_history(r, trip_id)
    assert hist[0]["turn"] == 2


@pytest.mark.asyncio
async def test_get_current_turn_empty_history_returns_zero():
    r = _make_redis()
    trip_id = str(uuid.uuid4())
    result = await get_current_turn(r, trip_id)
    assert result == 0


@pytest.mark.asyncio
async def test_get_current_turn_returns_highest_turn():
    r = _make_redis()
    trip_id = str(uuid.uuid4())
    await append_history(r, trip_id, role="user", content="plan", turn=1)
    await append_history(r, trip_id, role="assistant", content="done", turn=1)
    await append_history(r, trip_id, role="user", content="refine", turn=2)
    result = await get_current_turn(r, trip_id)
    assert result == 2


@pytest.mark.asyncio
async def test_append_history_defaults_turn_to_1():
    """Backward compatibility: callers that omit turn get turn=1."""
    r = _make_redis()
    trip_id = str(uuid.uuid4())
    await append_history(r, trip_id, role="user", content="Hello")
    hist = await get_history(r, trip_id)
    assert hist[0]["turn"] == 1


# ── Dev C — run_logger turn propagation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_log_agent_run_writes_turn():
    from src.ai.utils.run_logger import log_agent_run

    added = []
    db = AsyncMock()
    db.add = lambda obj: added.append(obj)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    trip_id = uuid.uuid4()
    run = await log_agent_run(
        db=db,
        trip_id=trip_id,
        agent_name="flight_agent",
        input={},
        output={},
        duration_ms=100,
        turn=2,
    )
    assert added[0].turn == 2


@pytest.mark.asyncio
async def test_get_retry_chain_includes_turn():
    from src.ai.utils.run_logger import get_retry_chain

    try:
        from app.models.agent_run import AgentRun
    except ImportError:
        from src.backend.app.models.agent_run import AgentRun

    from datetime import datetime, timezone

    trip_id = uuid.uuid4()
    fake_run = AgentRun(
        trip_id=trip_id,
        agent_name="flight_agent",
        status="completed",
        input={},
        output={},
        duration_ms=50,
        turn=2,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [fake_run]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=mock_result)

    chain = await get_retry_chain(db, trip_id)
    assert chain[0]["turn"] == 2


# ── Dev D — API endpoints ─────────────────────────────────────────────────────


def _auth_headers(user_id: uuid.UUID) -> dict:
    token = create_access_token({"sub": str(user_id)})
    return {"Authorization": f"Bearer {token}"}


def _make_app_mocks(user_id: uuid.UUID, trip_id: uuid.UUID):
    """Return the standard mock tuple for trip route tests."""
    try:
        from app.models.agent_run import AgentRun
        from app.models.trip import Trip, TripStatus
        from app.models.user import User
    except ImportError:
        from src.backend.app.models.agent_run import AgentRun
        from src.backend.app.models.trip import Trip, TripStatus
        from src.backend.app.models.user import User

    from datetime import datetime, timezone

    mock_user = User(id=user_id, email="u@test.com", hashed_password="x")
    mock_trip = MagicMock(spec=Trip)
    mock_trip.id = trip_id
    mock_trip.user_id = user_id
    mock_trip.status = TripStatus.COMPLETED

    run_t1 = AgentRun(trip_id=trip_id, agent_name="flight_agent", status="completed",
                      input={}, output={}, duration_ms=10, turn=1,
                      created_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).replace(tzinfo=None))
    run_t2 = AgentRun(trip_id=trip_id, agent_name="flight_agent", status="completed",
                      input={}, output={}, duration_ms=10, turn=2,
                      created_at=datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc).replace(tzinfo=None))

    return mock_user, mock_trip, run_t1, run_t2


@pytest.mark.asyncio
async def test_get_trip_runs_turn_filter():
    """GET /trips/{id}/runs?turn=1 returns only turn-1 rows."""
    from src.backend.app.main import app
    from app.api.deps import get_current_user, get_db

    user_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    mock_user, mock_trip, run_t1, run_t2 = _make_app_mocks(user_id, trip_id)

    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = mock_trip
    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = [run_t1]

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=[trip_result, runs_result])

    async def _override_db():
        yield mock_db

    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db] = _override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/trips/{trip_id}/runs?turn=1",
                headers=_auth_headers(user_id),
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert all(r["turn"] == 1 for r in data)


@pytest.mark.asyncio
async def test_get_trip_runs_no_filter_returns_all():
    """GET /trips/{id}/runs without ?turn returns all rows."""
    from src.backend.app.main import app
    from app.api.deps import get_current_user, get_db

    user_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    mock_user, mock_trip, run_t1, run_t2 = _make_app_mocks(user_id, trip_id)

    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = mock_trip
    runs_result = MagicMock()
    runs_result.scalars.return_value.all.return_value = [run_t1, run_t2]

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=[trip_result, runs_result])

    async def _override_db():
        yield mock_db

    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db] = _override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/trips/{trip_id}/runs",
                headers=_auth_headers(user_id),
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_refine_409_when_no_prior_state():
    """POST /trips/{id}/refine returns 409 if no Redis planning state exists."""
    from src.backend.app.main import app
    from app.api.deps import get_current_user, get_db, get_redis_dep

    user_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    mock_user, mock_trip, _, _ = _make_app_mocks(user_id, trip_id)

    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = mock_trip
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=trip_result)

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)  # no prior state

    async def _override_db():
        yield mock_db

    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis_dep] = lambda: mock_redis
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/trips/{trip_id}/refine",
                json={"message": "Make it cheaper"},
                headers=_auth_headers(user_id),
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_refine_returns_refinement_started():
    """POST /trips/{id}/refine returns 200 with refinement_started and correct turn."""
    import json as _json
    from src.backend.app.main import app
    from app.api.deps import get_current_user, get_db, get_redis_dep

    user_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    mock_user, mock_trip, _, _ = _make_app_mocks(user_id, trip_id)

    prior_state = {"destination": "Goa", "budget": 40000,
                   "start_date": "2026-12-10", "end_date": "2026-12-15"}

    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = mock_trip
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=trip_result)

    store = {
        f"trip:{trip_id}:planning_state": _json.dumps(prior_state),
        f"trip:{trip_id}:conv_history": _json.dumps(
            [{"role": "user", "content": "plan", "turn": 1}]
        ),
    }
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(side_effect=lambda k: store.get(k))
    mock_redis.setex = AsyncMock()

    mock_classification = RefinementClassification(
        refinement_type="targeted_flights",
        reason="User wants cheaper flights.",
    )

    async def _override_db():
        yield mock_db

    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis_dep] = lambda: mock_redis
    try:
        with (
            patch(
                "src.ai.agents.refinement_classifier.classify_refinement",
                AsyncMock(return_value=mock_classification),
            ),
            patch("asyncio.create_task", return_value=MagicMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    f"/trips/{trip_id}/refine",
                    json={"message": "Make it cheaper"},
                    headers=_auth_headers(user_id),
                )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "refinement_started"
    assert data["turn"] == 2
    assert data["refinement_type"] == "targeted_flights"


@pytest.mark.asyncio
async def test_list_itineraries_returns_all_versions_newest_first():
    """GET /trips/{id}/itineraries returns all rows ordered newest first."""
    from src.backend.app.main import app
    from app.api.deps import get_current_user, get_db
    from app.models.itinerary import Itinerary
    from datetime import datetime, timezone

    user_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    mock_user, mock_trip, _, _ = _make_app_mocks(user_id, trip_id)

    itin1 = Itinerary(trip_id=trip_id, structured_data={"days": []}, total_cost=40000,
                      created_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).replace(tzinfo=None))
    itin2 = Itinerary(trip_id=trip_id, structured_data={"days": []}, total_cost=38000,
                      created_at=datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc).replace(tzinfo=None))

    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = mock_trip
    itins_result = MagicMock()
    # Newest first — itin2 before itin1
    itins_result.scalars.return_value.all.return_value = [itin2, itin1]

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=[trip_result, itins_result])

    async def _override_db():
        yield mock_db

    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db] = _override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/trips/{trip_id}/itineraries",
                headers=_auth_headers(user_id),
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    # Newest (38000) should come first
    assert data[0]["total_cost"] == 38000


@pytest.mark.asyncio
async def test_run_orchestrator_saves_state_to_redis():
    """_run_orchestrator saves the final planning state to Redis after completion."""
    import json as _json
    from src.backend.app.api.routes.trips import _run_orchestrator

    trip_id = uuid.uuid4()
    saved_state = {}

    mock_redis = AsyncMock()
    mock_redis.setex = AsyncMock(
        side_effect=lambda k, _ttl, v: saved_state.update({k: v})
    )

    mock_agent_result = {
        "destination": "Goa",
        "flights": [{"price_inr": 5000}],
        "hotels": [],
        "attractions": [],
        "flight_status": "completed",
        "hotel_status": "completed",
        "activities_status": "completed",
    }

    with patch(
        "src.ai.orchestrator.orchestrator.OrchestratorAgent.run",
        AsyncMock(return_value=mock_agent_result),
    ):
        await _run_orchestrator(
            trip_id=trip_id,
            initial_state={"destination": "Goa"},
            publish_fn=AsyncMock(),
            redis=mock_redis,
        )

    # Redis setex must have been called with the trip state key
    assert mock_redis.setex.called
    state_key = f"trip:{trip_id}:planning_state"
    assert state_key in saved_state
    saved = _json.loads(saved_state[state_key])
    assert saved["destination"] == "Goa"
    assert "itinerary_id" not in saved  # stripped before saving


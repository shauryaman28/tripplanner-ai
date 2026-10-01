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
    r.set = AsyncMock(side_effect=lambda k, v, ex=None: store.update({k: v}))
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
    await log_agent_run(
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

    run_t1 = AgentRun(
        trip_id=trip_id,
        agent_name="flight_agent",
        status="completed",
        input={},
        output={},
        duration_ms=10,
        turn=1,
        created_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).replace(tzinfo=None),
    )
    run_t2 = AgentRun(
        trip_id=trip_id,
        agent_name="flight_agent",
        status="completed",
        input={},
        output={},
        duration_ms=10,
        turn=2,
        created_at=datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc).replace(tzinfo=None),
    )

    return mock_user, mock_trip, run_t1, run_t2


@pytest.mark.asyncio
async def test_get_trip_runs_turn_filter():
    """GET /trips/{id}/runs?turn=1 returns only turn-1 rows."""
    from app.api.deps import get_current_user, get_db
    from src.backend.app.main import app

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
    from app.api.deps import get_current_user, get_db
    from src.backend.app.main import app

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
    from app.api.deps import get_current_user, get_db, get_redis_dep
    from src.backend.app.main import app

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

    from app.api.deps import get_current_user, get_db, get_redis_dep
    from src.backend.app.main import app

    user_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    mock_user, mock_trip, _, _ = _make_app_mocks(user_id, trip_id)

    prior_state = {"destination": "Goa", "budget": 40000, "start_date": "2026-12-10", "end_date": "2026-12-15"}

    trip_result = MagicMock()
    trip_result.scalar_one_or_none.return_value = mock_trip
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=trip_result)
    mock_db.add = MagicMock()

    store = {
        f"trip:{trip_id}:planning_state": _json.dumps(prior_state),
        f"trip:{trip_id}:conv_history": _json.dumps([{"role": "user", "content": "plan", "turn": 1}]),
    }
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(side_effect=lambda k: store.get(k))
    mock_redis.set = AsyncMock()

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
            patch("app.api.routes.trips.classify_refinement", AsyncMock(return_value=mock_classification)),
            patch("app.api.routes.trips.spawn", side_effect=lambda coro: coro.close()),
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
    from datetime import datetime, timezone

    from app.api.deps import get_current_user, get_db
    from app.models.itinerary import Itinerary
    from src.backend.app.main import app

    user_id = uuid.uuid4()
    trip_id = uuid.uuid4()
    mock_user, mock_trip, _, _ = _make_app_mocks(user_id, trip_id)

    itin1 = Itinerary(
        trip_id=trip_id,
        structured_data={"days": []},
        total_cost=40000,
        created_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).replace(tzinfo=None),
    )
    itin2 = Itinerary(
        trip_id=trip_id,
        structured_data={"days": []},
        total_cost=38000,
        created_at=datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc).replace(tzinfo=None),
    )

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
    """_run_orchestrator saves the final planning state to Redis after a successful run."""
    import json as _json

    from app.api.routes.trips import _run_orchestrator

    trip_id = uuid.uuid4()
    saved_state = {}

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock(side_effect=lambda k, v, ex=None: saved_state.update({k: v}))

    result = {"destination": "Goa", "flights": [{"price_inr": 5000}], "itinerary_id": uuid.uuid4()}

    with patch("app.api.routes.trips.AsyncSessionLocal", MagicMock(return_value=AsyncMock())):
        await _run_orchestrator(trip_id, mock_redis, AsyncMock(return_value=result))

    saved = _json.loads(saved_state[f"trip:{trip_id}:planning_state"])
    assert saved["destination"] == "Goa"
    assert "itinerary_id" not in saved  # stripped before saving


def _session_factory(trip):
    """AsyncSessionLocal stand-in whose session returns `trip` from db.get()."""
    db = AsyncMock()
    db.get = AsyncMock(return_value=trip)
    db.__aenter__.return_value = db
    return MagicMock(return_value=db), db


@pytest.mark.asyncio
async def test_run_orchestrator_crash_marks_trip_failed_and_notifies_sse():
    """A crash mid-run must not leave the trip stuck in 'planning' (Phase 13 lifecycle)."""
    from app.api.routes.trips import _run_orchestrator
    from app.models.trip import TripStatus

    trip = MagicMock(status=TripStatus.PLANNING)
    factory, _ = _session_factory(trip)
    mock_redis = AsyncMock()

    with patch("app.api.routes.trips.AsyncSessionLocal", factory):
        await _run_orchestrator(uuid.uuid4(), mock_redis, AsyncMock(side_effect=RuntimeError("LLM exploded")))

    assert trip.status == TripStatus.FAILED
    event = __import__("json").loads(mock_redis.publish.await_args.args[1])
    assert event["event"] == "planning_failed" and "LLM exploded" in event["error"]
    mock_redis.set.assert_not_called()  # a failed run never overwrites the last good state


@pytest.mark.asyncio
async def test_failed_refinement_keeps_trip_completed_and_previous_state():
    """A refinement that produces no itinerary leaves the earlier itinerary standing."""
    from app.api.routes.trips import _run_orchestrator
    from app.models.trip import TripStatus

    trip = MagicMock(status=TripStatus.FAILED)  # what the graph's failure nodes set
    factory, _ = _session_factory(trip)
    mock_redis = AsyncMock()

    with patch("app.api.routes.trips.AsyncSessionLocal", factory):
        await _run_orchestrator(
            uuid.uuid4(), mock_redis, AsyncMock(return_value={"itinerary_id": None}), turn=2, has_itinerary=True
        )

    assert trip.status == TripStatus.COMPLETED
    mock_redis.set.assert_not_called()


# ── OrchestratorAgent.refine (selective re-run) ───────────────────────────────

_PRIOR = {
    "destination": "Goa",
    "origin": "DEL",
    "start_date": "2026-12-10",
    "end_date": "2026-12-12",
    "budget": 50_000.0,
    "group_size": 2,
    "interests": ["beach"],
    "flights": [{"airline": "6E", "price_inr": 8_000.0}],
    "hotels": [{"name": "Old Inn", "price_per_night_inr": 3_000.0}],
    "attractions": [{"name": "Fort Aguada"}],
    "flight_status": "completed",
    "hotel_status": "completed",
    "activities_status": "completed",
    "budget_decision": {
        "decision": "continue",
        "remaining_budget": 42_000.0,
        "flight_cost": 8_000.0,
        "total_budget": 50_000.0,
        "reason": "ok",
    },
    "evaluator_retry_count": 2,  # left over from turn 1 — must not eat into this turn's retries
    "draft_itinerary": {"days": [], "total_cost": 0},
}
_DRAFT = {
    "days": [{"day": 1, "date": "2026-12-10", "hotel": {"name": "Beach House", "cost_per_night": 4_000.0}}],
    "total_cost": 12_000.0,
    "currency": "INR",
}


def _orchestrator_mocks(stack, **agent_results):
    """Patch the three sub-agents + builder inside the orchestrator module; return their mocks."""
    mocks = {}
    for name in ("FlightAgent", "HotelAgent", "ActivitiesAgent", "ItineraryBuilder"):
        mocks[name] = stack.enter_context(patch(f"src.ai.orchestrator.orchestrator.{name}")).return_value
        mocks[name].run = AsyncMock(return_value=agent_results.get(name, {}))
    return mocks


@pytest.mark.asyncio
async def test_refine_targeted_hotel_reruns_only_hotel_agent_and_keeps_flights():
    """Phase 15 acceptance: "change hotels" → only HotelAgent runs; flights and activities carried forward."""
    from contextlib import ExitStack

    from src.ai.orchestrator.orchestrator import OrchestratorAgent

    published = []
    with ExitStack() as stack:
        mocks = _orchestrator_mocks(
            stack,
            HotelAgent={"hotels": [{"name": "Beach House", "price_per_night_inr": 4_000.0}], "error": None},
            ItineraryBuilder={"draft": _DRAFT, "error": None},
        )

        async def publish(event):
            published.append(event)

        result = await OrchestratorAgent().refine(
            "targeted_hotel", _PRIOR, "closer to the beach", publish_fn=publish, turn=2
        )

    mocks["HotelAgent"].run.assert_awaited_once()
    mocks["FlightAgent"].run.assert_not_called()
    mocks["ActivitiesAgent"].run.assert_not_called()
    assert mocks["HotelAgent"].run.await_args.kwargs["turn"] == 2

    assert result["flights"] == _PRIOR["flights"]
    assert result["attractions"] == _PRIOR["attractions"]
    assert result["hotels"][0]["name"] == "Beach House"
    assert result["evaluator_verdict"]["passed"] is True
    assert result["evaluator_retry_count"] == 0

    # the user's request reaches the builder, and the refinement is announced over SSE
    assert mocks["ItineraryBuilder"].run.await_args.kwargs["trip_meta"]["request"] == "closer to the beach"
    assert [e.get("agent") or e.get("event") for e in published] == ["hotel_agent", "planning_complete"]


@pytest.mark.asyncio
async def test_refine_targeted_hotel_failure_keeps_previous_hotels():
    from contextlib import ExitStack

    from src.ai.orchestrator.orchestrator import OrchestratorAgent

    with ExitStack() as stack:
        _orchestrator_mocks(
            stack,
            HotelAgent={"hotels": [], "error": {"error": "down", "code": "API_NOT_CONFIGURED"}},
            ItineraryBuilder={"draft": None, "error": {"error": "x", "code": "LLM_ERROR"}},
        )
        result = await OrchestratorAgent().refine("targeted_hotel", _PRIOR, "nicer hotel")

    assert result["hotels"] == _PRIOR["hotels"]


@pytest.mark.asyncio
async def test_refine_targeted_flights_runs_budget_check_and_escalates_on_conflict():
    """Hard case "make it cheaper": targeted_flights, then the budget decision node."""
    from contextlib import ExitStack

    from src.ai.orchestrator.orchestrator import OrchestratorAgent

    published = []

    async def publish(event):
        published.append(event)

    with ExitStack() as stack:
        mocks = _orchestrator_mocks(stack, FlightAgent={"flights": [{"price_inr": 40_000.0}], "error": None})
        result = await OrchestratorAgent().refine("targeted_flights", _PRIOR, "make it cheaper", publish_fn=publish)

    assert result["budget_decision"]["decision"] == "escalate"
    assert "budget_conflict" in [e.get("event") for e in published]
    mocks["ItineraryBuilder"].run.assert_not_called()
    mocks["HotelAgent"].run.assert_not_called()


@pytest.mark.asyncio
async def test_refine_targeted_activities_takes_new_interests_from_the_message():
    from contextlib import ExitStack

    from src.ai.orchestrator.orchestrator import OrchestratorAgent

    with ExitStack() as stack:
        mocks = _orchestrator_mocks(
            stack,
            ActivitiesAgent={"attractions": [{"name": "Mapusa Market"}], "error": None},
            ItineraryBuilder={"draft": _DRAFT, "error": None},
        )
        stack.enter_context(
            patch(
                "src.ai.orchestrator.orchestrator._extract_intent", AsyncMock(return_value={"interests": ["shopping"]})
            )
        )
        result = await OrchestratorAgent().refine("targeted_activities", _PRIOR, "swap the beach for shopping")

    assert mocks["ActivitiesAgent"].run.await_args.args[0]["interests"] == ["shopping"]
    assert result["interests"] == ["shopping"]
    mocks["FlightAgent"].run.assert_not_called()


@pytest.mark.asyncio
async def test_refine_full_replan_resets_state_and_lets_the_message_override_destination():
    """Hard case "I'd rather go to Mumbai": all agents, reset state, new destination wins."""
    from src.ai.orchestrator.orchestrator import OrchestratorAgent

    agent = OrchestratorAgent()
    with patch.object(agent, "run", AsyncMock(return_value={})) as mock_run:
        await agent.refine("full_replan", _PRIOR, "I'd rather go to Mumbai", turn=2)

    state = mock_run.await_args.args[0]
    assert state["raw_input"] == "I'd rather go to Mumbai" and state["intent_override"] is True
    assert "flights" not in state and "draft_itinerary" not in state and "evaluator_retry_count" not in state
    assert mock_run.await_args.kwargs["turn"] == 2


@pytest.mark.asyncio
async def test_refine_add_day_extends_end_date_and_replans_everything():
    """Hard case "add a day": all three agents re-run on the longer date range."""
    from src.ai.orchestrator.orchestrator import OrchestratorAgent

    agent = OrchestratorAgent()
    with patch.object(agent, "run", AsyncMock(return_value={})) as mock_run:
        await agent.refine("add_day", _PRIOR, "add a day", turn=3)

    state = mock_run.await_args.args[0]
    assert state["end_date"] == "2026-12-13"
    assert "flights" not in state and "raw_input" not in state


@pytest.mark.asyncio
async def test_intent_override_replaces_destination_and_keeps_trip_length():
    from src.ai.orchestrator.orchestrator import intent_parsing_node

    state = {
        "destination": "Goa",
        "start_date": "2026-12-10",
        "end_date": "2026-12-15",
        "budget": 50_000.0,
        "raw_input": "Mumbai in January instead",
    }
    parsed = {"destination": "Mumbai", "start_date": "2027-01-05"}

    with patch("src.ai.orchestrator.orchestrator._extract_intent", AsyncMock(return_value=parsed)):
        kept = await intent_parsing_node(state)
        replaced = await intent_parsing_node({**state, "intent_override": True})

    assert kept["destination"] == "Goa" and kept["start_date"] == "2026-12-10"  # fill-only by default
    assert replaced["destination"] == "Mumbai"
    assert (replaced["start_date"], replaced["end_date"]) == ("2027-01-05", "2027-01-10")  # still 5 nights

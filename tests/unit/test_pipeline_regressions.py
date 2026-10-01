"""Regression tests for end-to-end bugs that the per-phase suites could not see
because each of them mocked the layer where the bug lived.

  MCP client     list results were truncated to their first element
  evaluator      itinerary cost was compared to the user's budget, not to source prices
  budget node    a flight-provider outage was reported as a budget conflict
  orchestrator   failed flight searches were silent on SSE; sub-agent clarifications looked like success
  trip routes    clarification contract, one-run-at-a-time, replan persistence, status scoping, error envelope
"""

import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.types import CallToolResult, TextContent

from app.models.agent_run import AgentRun
from app.models.trip import Trip, TripStatus
from src.ai.agents.budget_decision import make_budget_decision
from src.ai.agents.evaluator import evaluate_itinerary, expected_total_cost
from src.ai.mcp_client.client import call_tool
from src.ai.orchestrator.orchestrator import hotel_activities_node, run_flight_node

START = date.today() + timedelta(days=30)

# ── MCP client ─────────────────────────────────────────────────────────────


async def _call_with(response: CallToolResult):
    session = AsyncMock()
    session.call_tool.return_value = response
    with patch("src.ai.mcp_client.client.get_session", return_value=session):
        return await call_tool("search_flights", {})


@pytest.mark.asyncio
async def test_call_tool_returns_every_item_of_a_list_result():
    """FastMCP sends one text block per list item — all of them must come back."""
    blocks = [TextContent(type="text", text=f'{{"airline": "{code}"}}') for code in ("6E", "AI", "UK")]
    result = await _call_with(CallToolResult(content=blocks))
    assert [f["airline"] for f in result] == ["6E", "AI", "UK"]


@pytest.mark.asyncio
async def test_call_tool_prefers_structured_content_and_unwraps_result():
    structured = CallToolResult(
        content=[TextContent(type="text", text='{"airline": "6E"}')],
        structuredContent={"result": [{"airline": "6E"}]},
    )
    assert await _call_with(structured) == [{"airline": "6E"}]  # a one-item list stays a list

    empty = CallToolResult(content=[], structuredContent={"result": []})
    assert await _call_with(empty) == []

    error = CallToolResult(content=[], structuredContent={"result": {"error": "nope", "code": "NO_RESULTS"}})
    assert (await _call_with(error)).code == "NO_RESULTS"


# ── Evaluator budget check ─────────────────────────────────────────────────

_DRAFT = {
    "days": [
        {
            "day": 1,
            "date": "2026-12-10",
            "morning": {"activity": "Fort Aguada", "cost": 500},
            "hotel": {"name": "Goa Grand", "cost_per_night": 4_500.0},
        },
        {"day": 2, "date": "2026-12-11", "hotel": {"name": "Goa Grand", "cost_per_night": 4_500.0}},
    ],
    "total_cost": 17_700.0,
}
_FLIGHTS = [{"price_inr": 9_000.0}, {"price_inr": 8_200.0}]
_HOTELS = [{"name": "Goa Grand", "price_per_night_inr": 4_500.0}]
_ATTRACTIONS = [{"name": "Fort Aguada"}]


def test_expected_total_cost_uses_source_prices():
    assert expected_total_cost(_DRAFT, _FLIGHTS, _HOTELS) == 8_200 + 2 * 4_500 + 500
    assert expected_total_cost(_DRAFT, [], []) == 2 * 4_500 + 500  # no flights; hotel falls back to the draft's price


def test_itinerary_far_below_the_users_budget_still_passes():
    """A ₹17,700 plan on a ₹50,000 budget is a good plan, not a budget_mismatch."""
    expected = expected_total_cost(_DRAFT, _FLIGHTS, _HOTELS)
    assert evaluate_itinerary(_DRAFT, "2026-12-10", "2026-12-12", expected, _ATTRACTIONS).passed


def test_misquoted_hotel_price_is_a_budget_mismatch():
    cheap = {
        **_DRAFT,
        "days": [{**d, "hotel": {"name": "Goa Grand", "cost_per_night": 1_000.0}} for d in _DRAFT["days"]],
        "total_cost": 10_700.0,
    }
    verdict = evaluate_itinerary(
        cheap, "2026-12-10", "2026-12-12", expected_total_cost(cheap, _FLIGHTS, _HOTELS), _ATTRACTIONS
    )
    assert [f.check for f in verdict.failures] == ["budget_mismatch"]


# ── Budget decision ────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", ["API_NOT_CONFIGURED", "DUFFEL_ERROR", "UNKNOWN_DESTINATION", "MCP_CLIENT_ERROR"])
def test_flight_provider_outage_continues_without_flights(code):
    decision = make_budget_decision([], 40_000, flight_error={"error": "x", "code": code})
    assert decision.decision == "continue"
    assert decision.remaining_budget == 40_000 and decision.flight_cost == 0


@pytest.mark.parametrize(
    "error", [None, {"error": "x", "code": "NO_RESULTS"}, {"error": "x", "code": "BUDGET_TOO_LOW"}]
)
def test_no_affordable_flights_is_still_a_budget_conflict(error):
    assert make_budget_decision([], 40_000, flight_error=error).decision == "escalate"


# ── Orchestrator nodes ─────────────────────────────────────────────────────

_STATE = {
    "destination": "Goa",
    "start_date": "2026-12-10",
    "end_date": "2026-12-12",
    "budget": 50_000.0,
    "group_size": 2,
}


@pytest.mark.asyncio
async def test_failed_flight_search_is_published_and_replan_loosens_the_search():
    published = []

    async def publish(event):
        published.append(event)

    with patch("src.ai.orchestrator.orchestrator.FlightAgent") as MockFA:
        MockFA.return_value.run = AsyncMock(
            return_value={"flights": [], "error": {"error": "Duffel down", "code": "DUFFEL_ERROR"}}
        )
        result = await run_flight_node({**_STATE, "publish_fn": publish, "replan_attempts": 1})

    assert result["flight_status"] == "failed" and result["flight_error"]["code"] == "DUFFEL_ERROR"
    assert published == [{"agent": "flight_agent", "status": "failed", "summary": "Duffel down"}]

    sent = MockFA.return_value.run.await_args.args[0]
    assert sent["budget"] == 50_000 * 0.65 and sent["max_stops"] == 2  # re-plan: tighter cap, one more stop
    assert sent["return_date"] == "2026-12-12"


@pytest.mark.asyncio
async def test_missing_interests_default_to_sightseeing_and_subagent_clarification_is_a_failure():
    with (
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as MockHA,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as MockAA,
    ):
        MockHA.return_value.run = AsyncMock(
            return_value={"hotels": [], "error": None, "clarification_question": "Budget?"}
        )
        MockAA.return_value.run = AsyncMock(return_value={"attractions": [{"name": "Fort Aguada"}], "error": None})
        result = await hotel_activities_node({**_STATE, "interests": []})

    assert MockAA.return_value.run.await_args.args[0]["interests"] == ["sightseeing"]
    assert result["activities_status"] == "completed"
    assert result["hotel_status"] == "failed" and result["hotel_error"]["code"] == "MISSING_INPUT"


# ── Trip routes ────────────────────────────────────────────────────────────


def _trip(user_id: uuid.UUID, status: str = "pending", interests=("beach",), days: int = 7) -> Trip:
    return Trip(
        id=uuid.uuid4(),
        user_id=user_id,
        destination="Goa",
        start_date=START,
        end_date=START + timedelta(days=days),
        budget=40_000.0,
        group_size=2,
        interests=list(interests),
        status=status,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


@contextmanager
def _client(trip: Trip | None, runs: list[AgentRun] | None = None):
    """App client with auth, DB and Redis overridden; background runs are captured, not executed."""
    from app.api.deps import get_current_user, get_db, get_redis_dep
    from app.main import app

    db = AsyncMock()
    db.add = MagicMock()
    trip_result, runs_result = MagicMock(), MagicMock()
    trip_result.scalar_one_or_none.return_value = trip
    runs_result.scalars.return_value.all.return_value = runs or []
    db.execute = AsyncMock(side_effect=[trip_result, runs_result])
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)

    async def _db():
        yield db

    app.dependency_overrides.update(
        {
            get_current_user: lambda: MagicMock(id=trip.user_id if trip else uuid.uuid4()),
            get_db: _db,
            get_redis_dep: lambda: redis,
        }
    )
    try:
        with patch("app.api.routes.trips.spawn") as spawn:
            spawn.side_effect = lambda coro: coro.close()
            yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), spawn, redis
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_plan_asks_for_interests_instead_of_assuming():
    """Phase 7B contract: nothing to infer interests from → clarification_needed, no run started."""
    trip = _trip(uuid.uuid4(), interests=())
    with _client(trip) as (client, spawn, _):
        resp = await client.post(f"/trips/{trip.id}/plan")

    assert resp.status_code == 200
    assert resp.json()["status"] == "clarification_needed" and "activities" in resp.json()["question"]
    spawn.assert_not_called()
    assert trip.status == "pending"


@pytest.mark.asyncio
async def test_plan_with_free_text_starts_planning_even_without_interests():
    trip = _trip(uuid.uuid4(), interests=())
    with _client(trip) as (client, spawn, redis):
        resp = await client.post(f"/trips/{trip.id}/plan", json={"raw_input": "beaches and seafood please"})

    assert resp.status_code == 202 and resp.json()["status"] == "planning_started"
    spawn.assert_called_once()
    assert trip.status == TripStatus.PLANNING
    assert '"planning_started"' in redis.publish.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,body",
    [
        ("plan", {}),
        ("clarify", {"answer": "beaches"}),
        ("replan", {"choice": "increase_budget"}),
        ("refine", {"message": "cheaper"}),
    ],
)
async def test_only_one_planning_run_per_trip(path, body):
    trip = _trip(uuid.uuid4(), status="planning")
    with _client(trip) as (client, spawn, _):
        resp = await client.post(f"/trips/{trip.id}/{path}", json=body)

    assert resp.status_code == 409
    assert resp.json()["error"] == {"code": "CONFLICT", "message": "Planning is already in progress for this trip."}
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_replan_persists_the_chosen_adjustment():
    trip = _trip(uuid.uuid4(), status="failed")
    with _client(trip) as (client, _, redis):
        resp = await client.post(f"/trips/{trip.id}/replan", json={"choice": "increase_budget"})

    assert resp.status_code == 200
    assert trip.budget == 50_000.0  # saved on the trip, so a second "increase_budget" compounds
    assert '"choice": "increase_budget"' in redis.publish.await_args.args[1]


@pytest.mark.asyncio
async def test_replan_reduce_days_rejects_a_trip_that_is_too_short():
    trip = _trip(uuid.uuid4(), status="failed", days=2)
    with _client(trip) as (client, spawn, _):
        resp = await client.post(f"/trips/{trip.id}/replan", json={"choice": "reduce_days"})

    assert resp.status_code == 422
    assert trip.end_date == START + timedelta(days=2)
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_get_trip_returns_the_trip_or_404():
    trip = _trip(uuid.uuid4())
    with _client(trip) as (client, _, __):
        resp = await client.get(f"/trips/{trip.id}")
    assert resp.status_code == 200 and resp.json()["destination"] == "Goa"

    with _client(None) as (client, _, __):
        resp = await client.get(f"/trips/{uuid.uuid4()}")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "NOT_FOUND"


def _run(trip_id: uuid.UUID, agent_name: str, status: str = "completed") -> AgentRun:
    return AgentRun(trip_id=trip_id, agent_name=agent_name, status=status, turn=1)


@pytest.mark.asyncio
async def test_status_counts_only_the_run_in_flight():
    """Re-planning a finished trip must start again from 0/3, not show the previous run as done."""
    trip = _trip(uuid.uuid4(), status="planning")
    previous = [_run(trip.id, name) for name in ("flight_agent", "hotel_agent", "activities_agent", "orchestrator")]
    current = [_run(trip.id, "flight_agent", "failed")]

    with _client(trip, previous + current) as (client, _, __):
        progress = (await client.get(f"/trips/{trip.id}/status")).json()["progress"]

    assert progress["agents"] == {"flight_agent": "failed", "hotel_agent": "pending", "activities_agent": "pending"}
    assert progress["agents_done"] == 0


@pytest.mark.asyncio
async def test_validation_errors_use_the_error_envelope_and_reject_past_trips():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    body = {"destination": "Goa", "start_date": yesterday, "end_date": str(START), "budget": 40_000}
    with _client(None) as (client, _, __):
        resp = await client.post("/trips", json=body)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR" and "start_date" in error["message"]
    assert isinstance(resp.json()["detail"], list)  # FastAPI's own shape is still there


@pytest.mark.asyncio
async def test_embedding_health_reports_backlog():
    with _client(None) as (client, _, redis):
        from app.api.deps import get_db
        from app.main import app

        db = AsyncMock()
        db.scalar = AsyncMock(return_value=2)

        async def _db():
            yield db

        app.dependency_overrides[get_db] = _db
        redis.get = AsyncMock(return_value="1")
        resp = await client.get("/admin/embedding-health")

    assert resp.json() == {"status": "degraded", "in_flight": 1, "pending_retry": 2}

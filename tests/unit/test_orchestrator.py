"""
Unit tests for Phase 9, 10, and 12 OrchestratorAgent.

Phase 9 (preserved):
  - intent_parsing_node pass-through
  - full happy path via OrchestratorAgent.run()

Phase 10:
  - run_flight_node, budget_decision_node, hotel_activities_node, escalate_node
  - partial failure in hotel+activities
  - all-agents-fail (no exception)
  - SSE events across full sequential node chain
  - escalate path when flights are expensive
  - replan_attempts cap forces escalate

Phase 12:
  - build_itinerary_node, evaluate_node, retry_dispatch_node, persist_node
  - route_after_evaluator branches (passed, retry, failed)
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.ai.agents.evaluator import EvaluatorVerdict
from src.ai.orchestrator.orchestrator import (
    OrchestratorAgent,
    OrchestratorState,
    budget_decision_node,
    build_itinerary_node,
    evaluate_node,
    hotel_activities_node,
    intent_parsing_node,
    merge_node,
    persist_node,
    retry_dispatch_node,
    route_after_evaluator,
    run_flight_node,
)

# ── Fixtures ───────────────────────────────────────────────────────────────

_FLIGHT = {
    "airline": "6E", "flight_number": "6E-204",
    "departure": "2026-12-10T06:00:00", "arrival": "2026-12-10T08:15:00",
    "duration_mins": 135, "price_inr": 8200.0, "stops": 0,
}
_HOTEL = {"name": "Goa Grand", "stars": 4, "price_per_night_inr": 4500.0, "rating": 4.2, "address": "Goa"}
_ATTRACTION = {"name": "Fort Aguada", "category": "history", "rating": 4.5,
               "description": "17th-century fort.", "lat": 15.5, "lng": 73.7}

_GOOD_DRAFT = {
    "days": [{
        "day": 1, "date": "2026-12-10",
        "morning": {"activity": "Fort Aguada", "cost": 0, "lat": 15.5, "lng": 73.7},
        "afternoon": None, "evening": None,
        "hotel": {"name": "Goa Grand", "cost_per_night": 4500.0},
        "flight": None,
    }],
    "total_cost": 12700.0,
    "currency": "INR",
}

_BASE_STATE: OrchestratorState = {
    "destination": "Goa",
    "origin": "DEL",
    "start_date": "2026-12-10",
    "end_date": "2026-12-17",
    "budget": 50000.0,
    "group_size": 2,
    "interests": ["beach", "food"],
    "publish_fn": None,
    "db": None,
    "trip_id": None,
    "flights": [],
    "hotels": [],
    "attractions": [],
    "flight_status": "skipped",
    "hotel_status": "skipped",
    "activities_status": "skipped",
    "flight_error": None,
    "hotel_error": None,
    "activities_error": None,
    "replan_attempts": 0,
    "budget_decision": None,
    "budget_conflict_options": None,
    "draft_itinerary": None,
    "builder_error": None,
    "evaluator_verdict": None,
    "evaluator_retry_count": 0,
    "itinerary_id": None,
}

# State as it arrives at hotel_activities_node (after flight + budget check)
_AFTER_BUDGET_CHECK: OrchestratorState = {
    **_BASE_STATE,
    "flights": [_FLIGHT],
    "flight_status": "completed",
    "budget_decision": {
        "decision": "continue",
        "reason": "Budget check passed.",
        "remaining_budget": 41_800.0,
        "flight_cost": 8_200.0,
        "total_budget": 50_000.0,
    },
}


# ── Test 1: intent_parsing_node pass-through (Phase 9) ────────────────────


@pytest.mark.asyncio
async def test_intent_parsing_no_raw_input_passthrough():
    """No raw_input → state unchanged, no LLM call."""
    with patch("src.ai.orchestrator.orchestrator.ChatGoogleGenerativeAI") as MockLLM:
        result = await intent_parsing_node(_BASE_STATE)
    MockLLM.assert_not_called()
    assert result["destination"] == "Goa"
    assert result["start_date"] == "2026-12-10"


# ── Test 2: full happy path via OrchestratorAgent.run() (Phase 9 + 10 + 12) ──


@pytest.mark.asyncio
async def test_orchestrator_full_happy_path():
    """All 3 sub-agents succeed; budget check passes; builder + evaluator pass; itinerary persisted (no db → itinerary_id stays None)."""
    with (
        patch("src.ai.orchestrator.orchestrator.FlightAgent") as MockFA,
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as MockHA,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as MockAA,
        patch("src.ai.orchestrator.orchestrator.ItineraryBuilder") as MockIB,
        patch("src.ai.orchestrator.orchestrator.EvaluatorAgent") as MockEval,
    ):
        MockFA.return_value.run = AsyncMock(return_value={"flights": [_FLIGHT], "error": None})
        MockHA.return_value.run = AsyncMock(return_value={"hotels": [_HOTEL], "error": None})
        MockAA.return_value.run = AsyncMock(return_value={"attractions": [_ATTRACTION], "error": None})
        MockIB.return_value.run = AsyncMock(return_value={"draft": _GOOD_DRAFT, "error": None})
        MockEval.return_value.run = AsyncMock(
            return_value=EvaluatorVerdict(passed=True, failures=[], retry_count=0)
        )

        agent = OrchestratorAgent()
        result = await agent.run(_BASE_STATE)

    assert result["flight_status"] == "completed"
    assert result["hotel_status"] == "completed"
    assert result["activities_status"] == "completed"
    assert result["draft_itinerary"] == _GOOD_DRAFT
    assert result["budget_decision"]["decision"] == "continue"
    # no db passed in this test → persist_node is a no-op, itinerary_id stays None
    assert result["itinerary_id"] is None


# ── Test 3: hotel_activities_node partial failure (Phase 10) ──────────────


@pytest.mark.asyncio
async def test_hotel_activities_partial_failure_continues():
    """HotelAgent fails → activities still returned, hotel_status='failed'."""
    hotel_result = {"hotels": [], "error": {"error": "Amadeus down", "code": "AMADEUS_ERROR"}}
    activities_result = {"attractions": [_ATTRACTION], "error": None}

    with (
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as MockHA,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as MockAA,
    ):
        MockHA.return_value.run = AsyncMock(return_value=hotel_result)
        MockAA.return_value.run = AsyncMock(return_value=activities_result)

        result = await hotel_activities_node(_AFTER_BUDGET_CHECK)

    assert result["hotel_status"] == "failed"
    assert result["activities_status"] == "completed"
    assert result["hotel_error"]["code"] == "AMADEUS_ERROR"
    assert len(result["attractions"]) == 1


# ── Test 4: hotel_activities_node all-fail (Phase 10) ─────────────────────


@pytest.mark.asyncio
async def test_hotel_activities_all_agents_fail_no_exception():
    """Both hotel and activities fail → no exception raised."""
    error_result = {"error": {"error": "API down", "code": "API_NOT_CONFIGURED"}, "hotels": [], "attractions": []}

    with (
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as MockHA,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as MockAA,
    ):
        MockHA.return_value.run = AsyncMock(return_value=error_result)
        MockAA.return_value.run = AsyncMock(return_value=error_result)

        result = await hotel_activities_node(_AFTER_BUDGET_CHECK)

    assert result["hotel_status"] == "failed"
    assert result["activities_status"] == "failed"
    # No exception — system remains alive
    assert "hotels" in result


# ── Test 5: SSE events published across full node chain (Phase 10) ────────


@pytest.mark.asyncio
async def test_sse_events_published_for_all_agents():
    """4 SSE events: flight_agent + hotel_agent + activities_agent + planning_complete."""
    published: list[dict] = []

    async def mock_publish(event: dict) -> None:
        published.append(event)

    state = {**_BASE_STATE, "publish_fn": mock_publish}

    with (
        patch("src.ai.orchestrator.orchestrator.FlightAgent") as MockFA,
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as MockHA,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as MockAA,
    ):
        MockFA.return_value.run = AsyncMock(return_value={"flights": [_FLIGHT], "error": None})
        MockHA.return_value.run = AsyncMock(return_value={"hotels": [_HOTEL], "error": None})
        MockAA.return_value.run = AsyncMock(return_value={"attractions": [_ATTRACTION], "error": None})

        s1 = await run_flight_node(state)
        s2 = await budget_decision_node(s1)          # budget passes → "continue"
        s3 = await hotel_activities_node(s2)
        await merge_node(s3)

    assert len(published) == 4
    agent_names = {e.get("agent") for e in published if "agent" in e}
    assert {"flight_agent", "hotel_agent", "activities_agent"} == agent_names
    complete = [e for e in published if e.get("event") == "planning_complete"]
    assert len(complete) == 1


# ── Test 6: budget_decision_node escalates on expensive flights (Phase 10) ─


@pytest.mark.asyncio
async def test_budget_decision_node_escalates_on_expensive_flights():
    """Flights at 70% of budget (₹35k / ₹50k) → remaining 30% < 35% → escalate."""
    state = {**_BASE_STATE, "flights": [{"price_inr": 35_000.0}]}
    result = await budget_decision_node(state)

    assert result["budget_decision"]["decision"] == "escalate"
    assert result["budget_decision"]["remaining_budget"] == 15_000.0
    assert result["budget_conflict_options"] is not None
    assert len(result["budget_conflict_options"]) == 3
    # replan_attempts not incremented on escalate
    assert result["replan_attempts"] == 0


# ── Test 7: budget_decision_node continues on cheap flights (Phase 10) ─────


@pytest.mark.asyncio
async def test_budget_decision_node_continues_on_cheap_flights():
    """Flights at 40% of budget (₹20k / ₹50k) → remaining 60% ≥ 50% → continue."""
    state = {**_BASE_STATE, "flights": [{"price_inr": 20_000.0}]}
    result = await budget_decision_node(state)

    assert result["budget_decision"]["decision"] == "continue"
    assert result["budget_decision"]["remaining_budget"] == 30_000.0
    assert result["budget_conflict_options"] is None
    assert result["replan_attempts"] == 0  # not incremented on continue


# ── Test 8: escalate path via full OrchestratorAgent.run() (Phase 10) ─────


@pytest.mark.asyncio
async def test_orchestrator_publishes_budget_conflict_on_escalate():
    """Expensive flight → budget_conflict SSE; hotel + activities NOT called."""
    expensive_flight = {**_FLIGHT, "price_inr": 35_000.0}  # 70% of ₹50k
    published: list[dict] = []

    async def mock_publish(event: dict) -> None:
        published.append(event)

    with (
        patch("src.ai.orchestrator.orchestrator.FlightAgent") as MockFA,
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as MockHA,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as MockAA,
    ):
        MockFA.return_value.run = AsyncMock(return_value={"flights": [expensive_flight], "error": None})
        MockHA.return_value.run = AsyncMock(return_value={"hotels": [_HOTEL], "error": None})
        MockAA.return_value.run = AsyncMock(return_value={"attractions": [_ATTRACTION], "error": None})

        agent = OrchestratorAgent()
        result = await agent.run({**_BASE_STATE, "publish_fn": mock_publish})

    conflict_events = [e for e in published if e.get("event") == "budget_conflict"]
    assert len(conflict_events) == 1
    assert conflict_events[0]["options"]

    # Hotel and activities agents must not have been called
    MockHA.return_value.run.assert_not_called()
    MockAA.return_value.run.assert_not_called()

    assert result["hotel_status"] == "skipped"
    assert result["activities_status"] == "skipped"
    assert result["budget_decision"]["decision"] == "escalate"


# ── Test 9: replan cap forces escalate (Phase 10) ─────────────────────────


@pytest.mark.asyncio
async def test_replan_attempts_cap_forces_escalate():
    """replan_attempts already at MAX (2) → budget_decision escalates regardless."""
    state = {
        **_BASE_STATE,
        "flights": [{"price_inr": 27_500.0}],
        "replan_attempts": 2,  # at MAX_REPLAN_ATTEMPTS
    }
    result = await budget_decision_node(state)

    assert result["budget_decision"]["decision"] == "escalate"
    assert "Maximum re-planning" in result["budget_decision"]["reason"]
    assert result["replan_attempts"] == 2


# ── New Phase 12 tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_itinerary_node_populates_draft():
    with patch("src.ai.orchestrator.orchestrator.ItineraryBuilder") as MockIB:
        MockIB.return_value.run = AsyncMock(return_value={"draft": _GOOD_DRAFT, "error": None})
        result = await build_itinerary_node(_AFTER_BUDGET_CHECK)

    assert result["draft_itinerary"] == _GOOD_DRAFT
    assert result["builder_error"] is None


@pytest.mark.asyncio
async def test_evaluate_node_passes_through_verdict():
    state = {**_AFTER_BUDGET_CHECK, "draft_itinerary": _GOOD_DRAFT, "budget": 12700.0}
    with patch("src.ai.orchestrator.orchestrator.EvaluatorAgent") as MockEval:
        MockEval.return_value.run = AsyncMock(
            return_value=EvaluatorVerdict(passed=True, failures=[], retry_count=0)
        )
        result = await evaluate_node(state)

    assert result["evaluator_verdict"]["passed"] is True


def test_route_after_evaluator_builder_failure_retries_under_cap():
    state = {"builder_error": {"error": "x", "code": "LLM_ERROR"}, "draft_itinerary": None, "evaluator_retry_count": 0}
    assert route_after_evaluator(state) == "retry"


def test_route_after_evaluator_builder_failure_fails_at_cap():
    state = {"builder_error": {"error": "x", "code": "LLM_ERROR"}, "draft_itinerary": None, "evaluator_retry_count": 3}
    assert route_after_evaluator(state) == "failed"


def test_route_after_evaluator_passed():
    state = {"builder_error": None, "draft_itinerary": _GOOD_DRAFT,
             "evaluator_verdict": {"passed": True, "failures": [], "retry_count": 0}}
    assert route_after_evaluator(state) == "passed"


@pytest.mark.asyncio
async def test_retry_dispatch_node_reruns_activities_and_increments_count():
    state = {
        **_AFTER_BUDGET_CHECK,
        "evaluator_verdict": {
            "passed": False,
            "failures": [{"check": "hallucinated_activity", "detail": "x"}],
            "retry_count": 0,
        },
        "evaluator_retry_count": 0,
    }
    with patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as MockAA:
        MockAA.return_value.run = AsyncMock(return_value={"attractions": [_ATTRACTION], "error": None})
        result = await retry_dispatch_node(state)

    assert result["evaluator_retry_count"] == 1
    assert result["attractions"] == [_ATTRACTION]


@pytest.mark.asyncio
async def test_persist_node_noop_without_db():
    state = {"draft_itinerary": _GOOD_DRAFT, "db": None, "trip_id": None}
    result = await persist_node(state)
    assert result["itinerary_id"] is None

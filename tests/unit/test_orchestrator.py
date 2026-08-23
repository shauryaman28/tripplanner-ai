"""
Unit tests for Phase 9 OrchestratorAgent.

All sub-agents and Gemini Flash are mocked — zero network, zero Docker.
5 tests per the Phase 9 done criterion.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.orchestrator.orchestrator import (
    OrchestratorAgent,
    OrchestratorState,
    fan_out_node,
    intent_parsing_node,
    merge_node,
)


# ── Helpers ────────────────────────────────────────────────────────────────

_FLIGHT = {"airline": "6E", "flight_number": "6E-204", "departure": "2026-12-10T06:00:00",
           "arrival": "2026-12-10T08:15:00", "duration_mins": 135, "price_inr": 8200.0, "stops": 0}
_HOTEL = {"name": "Goa Grand", "stars": 4, "price_per_night_inr": 4500.0, "rating": 4.2, "address": "Goa"}
_ATTRACTION = {"name": "Fort Aguada", "category": "history", "rating": 4.5,
               "description": "17th-century fort.", "lat": 15.5, "lng": 73.7}

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
}


# ── Test 1: intent_parsing_node passes through when no raw_input ───────────


@pytest.mark.asyncio
async def test_intent_parsing_no_raw_input_passthrough():
    """No raw_input → state unchanged, no LLM call."""
    state = {**_BASE_STATE}
    with patch("src.ai.orchestrator.orchestrator.ChatGoogleGenerativeAI") as MockLLM:
        result = await intent_parsing_node(state)
    MockLLM.assert_not_called()
    assert result["destination"] == "Goa"
    assert result["start_date"] == "2026-12-10"


# ── Test 2: full happy path — all 3 agents succeed concurrently ────────────


@pytest.mark.asyncio
async def test_orchestrator_full_happy_path():
    """All 3 sub-agents succeed → OrchestratorAgent returns merged state."""
    flight_result = {"flights": [_FLIGHT], "error": None}
    hotel_result = {"hotels": [_HOTEL], "error": None}
    activities_result = {"attractions": [_ATTRACTION], "error": None}

    with (
        patch("src.ai.orchestrator.orchestrator.FlightAgent") as MockFA,
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as MockHA,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as MockAA,
    ):
        MockFA.return_value.run = AsyncMock(return_value=flight_result)
        MockHA.return_value.run = AsyncMock(return_value=hotel_result)
        MockAA.return_value.run = AsyncMock(return_value=activities_result)

        agent = OrchestratorAgent()
        result = await agent.run(_BASE_STATE)

    assert result["flight_status"] == "completed"
    assert result["hotel_status"] == "completed"
    assert result["activities_status"] == "completed"
    assert len(result["flights"]) == 1
    assert len(result["hotels"]) == 1
    assert len(result["attractions"]) == 1
    assert result["flights"][0]["airline"] == "6E"


# ── Test 3: partial failure — one agent fails, others succeed ──────────────


@pytest.mark.asyncio
async def test_fan_out_partial_failure_continues():
    """HotelAgent fails → flights and activities still returned, hotel_status='failed'."""
    from src.ai.mcp_server.models import ToolError

    flight_result = {"flights": [_FLIGHT], "error": None}
    hotel_result = {"hotels": [], "error": {"error": "Amadeus down", "code": "AMADEUS_ERROR"}}
    activities_result = {"attractions": [_ATTRACTION], "error": None}

    with (
        patch("src.ai.orchestrator.orchestrator.FlightAgent") as MockFA,
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as MockHA,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as MockAA,
    ):
        MockFA.return_value.run = AsyncMock(return_value=flight_result)
        MockHA.return_value.run = AsyncMock(return_value=hotel_result)
        MockAA.return_value.run = AsyncMock(return_value=activities_result)

        result = await fan_out_node(_BASE_STATE)

    assert result["flight_status"] == "completed"
    assert result["hotel_status"] == "failed"
    assert result["activities_status"] == "completed"
    assert result["hotel_error"]["code"] == "AMADEUS_ERROR"
    assert len(result["flights"]) == 1
    assert len(result["attractions"]) == 1


# ── Test 4: all 3 agents fail — result is failed but no exception raised ───


@pytest.mark.asyncio
async def test_fan_out_all_agents_fail_no_exception():
    """All three agents fail → no exception raised, all statuses='failed'."""
    error_result = {"error": {"error": "API down", "code": "API_NOT_CONFIGURED"}, "flights": [], "hotels": [], "attractions": []}

    with (
        patch("src.ai.orchestrator.orchestrator.FlightAgent") as MockFA,
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as MockHA,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as MockAA,
    ):
        MockFA.return_value.run = AsyncMock(return_value=error_result)
        MockHA.return_value.run = AsyncMock(return_value=error_result)
        MockAA.return_value.run = AsyncMock(return_value=error_result)

        result = await fan_out_node(_BASE_STATE)

    assert result["flight_status"] == "failed"
    assert result["hotel_status"] == "failed"
    assert result["activities_status"] == "failed"
    # No exception was raised — system remains alive
    assert "flights" in result


# ── Test 5: SSE publish_fn is called for each agent + planning_complete ────


@pytest.mark.asyncio
async def test_sse_events_published_for_each_agent():
    """publish_fn called 3× in fan_out + 1× in merge = 4 total events."""
    flight_result = {"flights": [_FLIGHT], "error": None}
    hotel_result = {"hotels": [_HOTEL], "error": None}
    activities_result = {"attractions": [_ATTRACTION], "error": None}

    published_events = []

    async def mock_publish(event: dict) -> None:
        published_events.append(event)

    state_with_publish = {**_BASE_STATE, "publish_fn": mock_publish}

    with (
        patch("src.ai.orchestrator.orchestrator.FlightAgent") as MockFA,
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as MockHA,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as MockAA,
    ):
        MockFA.return_value.run = AsyncMock(return_value=flight_result)
        MockHA.return_value.run = AsyncMock(return_value=hotel_result)
        MockAA.return_value.run = AsyncMock(return_value=activities_result)

        await fan_out_node(state_with_publish)
        await merge_node({**state_with_publish, "flight_status": "completed",
                          "hotel_status": "completed", "activities_status": "completed"})

    # 3 agent events + 1 planning_complete
    assert len(published_events) == 4
    agent_names = {e.get("agent") for e in published_events if "agent" in e}
    assert "flight_agent" in agent_names
    assert "hotel_agent" in agent_names
    assert "activities_agent" in agent_names
    complete_events = [e for e in published_events if e.get("event") == "planning_complete"]
    assert len(complete_events) == 1

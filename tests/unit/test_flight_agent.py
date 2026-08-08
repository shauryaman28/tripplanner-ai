"""
Unit tests for Phase 6 FlightAgent.

All tests mock call_tool (or the settings it depends on) — zero network,
zero real Amadeus calls. Matches the pattern already used in
tests/unit/mcp/test_tools.py.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.ai.agents.flight_agent import FlightAgent, TripState, search_flights_node
from src.ai.mcp_server.models import Flight, ToolError

FUTURE_DATE = "2026-12-10"


def _sample_flight_dict() -> dict:
    return Flight(
        airline="6E",
        flight_number="6E-204",
        departure=f"{FUTURE_DATE}T06:00:00",
        arrival=f"{FUTURE_DATE}T08:15:00",
        duration_mins=135,
        price_inr=4200.0,
        stops=0,
    ).model_dump()


BASE_STATE: TripState = {
    "origin": "DEL",
    "destination": "GOI",
    "date": FUTURE_DATE,
    "budget": 20000,
    "passengers": 1,
}


# ── Happy paths (4) ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_node_happy_path_populates_flights():
    with patch(
        "src.ai.agents.flight_agent.call_tool",
        AsyncMock(return_value=[_sample_flight_dict()]),
    ):
        result = await search_flights_node(BASE_STATE)

    assert result["error"] is None
    assert len(result["flights"]) == 1
    assert result["flights"][0]["airline"] == "6E"


@pytest.mark.asyncio
async def test_node_forwards_correct_params_to_call_tool():
    mock_call = AsyncMock(return_value=[_sample_flight_dict()])
    with patch("src.ai.agents.flight_agent.call_tool", mock_call):
        await search_flights_node(BASE_STATE)

    mock_call.assert_awaited_once_with(
        "search_flights",
        {"origin": "DEL", "destination": "GOI", "date": FUTURE_DATE, "budget": 20000, "passengers": 1},
    )


@pytest.mark.asyncio
async def test_node_defaults_passengers_to_one_when_missing():
    state_without_passengers = {k: v for k, v in BASE_STATE.items() if k != "passengers"}
    mock_call = AsyncMock(return_value=[_sample_flight_dict()])
    with patch("src.ai.agents.flight_agent.call_tool", mock_call):
        await search_flights_node(state_without_passengers)

    assert mock_call.await_args.args[1]["passengers"] == 1


@pytest.mark.asyncio
async def test_flight_agent_run_returns_populated_state():
    with patch(
        "src.ai.agents.flight_agent.call_tool",
        AsyncMock(return_value=[_sample_flight_dict()]),
    ):
        agent = FlightAgent()
        result = await agent.run(BASE_STATE)

    assert result["flights"][0]["price_inr"] == 4200.0
    assert result["error"] is None


# ── Error cases (2) ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_node_handles_tool_error():
    tool_error = ToolError(error="Departure date is in the past.", code="PAST_DATE")
    with patch("src.ai.agents.flight_agent.call_tool", AsyncMock(return_value=tool_error)):
        result = await search_flights_node(BASE_STATE)

    assert result["flights"] == []
    assert result["error"]["code"] == "PAST_DATE"


@pytest.mark.asyncio
async def test_flight_agent_run_propagates_error_via_run():
    tool_error = ToolError(error="Amadeus API not configured.", code="API_NOT_CONFIGURED")
    with patch("src.ai.agents.flight_agent.call_tool", AsyncMock(return_value=tool_error)):
        agent = FlightAgent()
        result = await agent.run(BASE_STATE)

    assert result["flights"] == []
    assert result["error"]["code"] == "API_NOT_CONFIGURED"

"""
Unit tests for Phase 8 HotelAgent.

All tests mock call_tool — zero network, zero real Amadeus calls.
Matches the pattern used in tests/unit/test_flight_agent.py.

6 tests total:
  - 4 happy paths: node populates hotels, correct params forwarded,
                   guests defaults to 1, agent.run() returns populated state
  - 2 error cases: ToolError propagated by node and by agent.run()
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.ai.agents.hotel_agent import HotelAgent, HotelState, search_hotels_node
from src.ai.mcp_server.models import Hotel, ToolError


def _sample_hotel_dict() -> dict:
    return Hotel(
        name="Goa Grand",
        stars=4,
        price_per_night_inr=4_500.0,
        rating=4.2,
        address="Beach Road, Calangute, Goa",
    ).model_dump()


BASE_STATE: HotelState = {
    "destination": "Goa",
    "check_in": "2026-12-10",
    "check_out": "2026-12-17",
    "budget_per_night": 5_000,
    "guests": 2,
}


# ── Happy paths (4) ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_node_happy_path_populates_hotels():
    with patch(
        "src.ai.agents.hotel_agent.call_tool",
        AsyncMock(return_value=[_sample_hotel_dict()]),
    ):
        result = await search_hotels_node(BASE_STATE)

    assert result["error"] is None
    assert len(result["hotels"]) == 1
    assert result["hotels"][0]["name"] == "Goa Grand"


@pytest.mark.asyncio
async def test_node_forwards_correct_params_to_call_tool():
    mock_call = AsyncMock(return_value=[_sample_hotel_dict()])
    with patch("src.ai.agents.hotel_agent.call_tool", mock_call):
        await search_hotels_node(BASE_STATE)

    mock_call.assert_awaited_once_with(
        "search_hotels",
        {
            "destination": "Goa",
            "check_in": "2026-12-10",
            "check_out": "2026-12-17",
            "budget_per_night": 5_000,
            "guests": 2,
        },
    )


@pytest.mark.asyncio
async def test_node_defaults_guests_to_one_when_missing():
    state_without_guests = {k: v for k, v in BASE_STATE.items() if k != "guests"}
    mock_call = AsyncMock(return_value=[_sample_hotel_dict()])
    with patch("src.ai.agents.hotel_agent.call_tool", mock_call):
        await search_hotels_node(state_without_guests)

    assert mock_call.await_args.args[1]["guests"] == 1


@pytest.mark.asyncio
async def test_hotel_agent_run_returns_populated_state():
    with patch(
        "src.ai.agents.hotel_agent.call_tool",
        AsyncMock(return_value=[_sample_hotel_dict()]),
    ):
        agent = HotelAgent()
        result = await agent.run(BASE_STATE)

    assert result["hotels"][0]["price_per_night_inr"] == 4_500.0
    assert result["error"] is None


# ── Error cases (2) ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_node_handles_tool_error():
    tool_error = ToolError(
        error="check_out must be after check_in.",
        code="INVALID_DATES",
    )
    with patch("src.ai.agents.hotel_agent.call_tool", AsyncMock(return_value=tool_error)):
        result = await search_hotels_node(BASE_STATE)

    assert result["hotels"] == []
    assert result["error"]["code"] == "INVALID_DATES"


@pytest.mark.asyncio
async def test_hotel_agent_run_propagates_error_via_run():
    tool_error = ToolError(
        error="Amadeus API not configured.",
        code="API_NOT_CONFIGURED",
    )
    with patch("src.ai.agents.hotel_agent.call_tool", AsyncMock(return_value=tool_error)):
        agent = HotelAgent()
        result = await agent.run(BASE_STATE)

    assert result["hotels"] == []
    assert result["error"]["code"] == "API_NOT_CONFIGURED"

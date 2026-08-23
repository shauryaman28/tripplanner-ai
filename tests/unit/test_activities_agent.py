"""
Unit tests for Phase 8 ActivitiesAgent.

All tests mock call_tool — zero network, zero real Google Maps calls.
Matches the pattern used in tests/unit/test_flight_agent.py.

6 tests total:
  - 4 happy paths: node populates attractions, correct params forwarded
                   (including "history" + "street food" acceptance criterion),
                   limit defaults to 5, agent.run() returns populated state
  - 1 error case:  ToolError propagated by node
  - 1 behavioural: non-English interest ("खाना") passes through without crashing

Non-English interest documented behaviour:
  The intent_parsing_node instructs Gemini Flash to translate non-English
  interests to English where possible. However, get_attractions_node makes
  no such transformation — it forwards interests as-is to the MCP tool,
  which embeds them verbatim in the Google Maps query. Results may be fewer
  or less relevant for untranslated terms, but the system never crashes or
  raises. This is the documented and tested behaviour for Phase 8.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.ai.agents.activities_agent import (
    ActivitiesAgent,
    ActivitiesState,
    get_attractions_node,
)
from src.ai.mcp_server.models import Attraction, ToolError


def _sample_attraction_dict() -> dict:
    return Attraction(
        name="Fort Aguada",
        category="history",
        rating=4.5,
        description="17th-century Portuguese fort overlooking the Arabian Sea.",
        lat=15.5009,
        lng=73.7655,
    ).model_dump()


BASE_STATE: ActivitiesState = {
    "destination": "Goa",
    "interests": ["history", "street food"],
    "limit": 5,
}


# ── Happy paths (4) ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_node_happy_path_populates_attractions():
    with patch(
        "src.ai.agents.activities_agent.call_tool",
        AsyncMock(return_value=[_sample_attraction_dict()]),
    ):
        result = await get_attractions_node(BASE_STATE)

    assert result["error"] is None
    assert len(result["attractions"]) == 1
    assert result["attractions"][0]["name"] == "Fort Aguada"


@pytest.mark.asyncio
async def test_node_forwards_history_and_street_food_interests():
    """'I like history and street food in Goa' → get_attractions called with
    interests=['history', 'street food'] — the Phase 8 Dev B acceptance criterion."""
    mock_call = AsyncMock(return_value=[_sample_attraction_dict()])
    with patch("src.ai.agents.activities_agent.call_tool", mock_call):
        await get_attractions_node(BASE_STATE)

    mock_call.assert_awaited_once_with(
        "get_attractions",
        {
            "destination": "Goa",
            "interests": ["history", "street food"],
            "limit": 5,
        },
    )


@pytest.mark.asyncio
async def test_node_defaults_limit_to_five_when_missing():
    state_without_limit = {k: v for k, v in BASE_STATE.items() if k != "limit"}
    mock_call = AsyncMock(return_value=[_sample_attraction_dict()])
    with patch("src.ai.agents.activities_agent.call_tool", mock_call):
        await get_attractions_node(state_without_limit)

    assert mock_call.await_args.args[1]["limit"] == 5


@pytest.mark.asyncio
async def test_activities_agent_run_returns_populated_state():
    with patch(
        "src.ai.agents.activities_agent.call_tool",
        AsyncMock(return_value=[_sample_attraction_dict()]),
    ):
        agent = ActivitiesAgent()
        result = await agent.run(BASE_STATE)

    assert result["attractions"][0]["category"] == "history"
    assert result["error"] is None


# ── Error case (1) ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_node_handles_tool_error():
    tool_error = ToolError(
        error="Google Maps API not configured.",
        code="API_NOT_CONFIGURED",
    )
    with patch("src.ai.agents.activities_agent.call_tool", AsyncMock(return_value=tool_error)):
        result = await get_attractions_node(BASE_STATE)

    assert result["attractions"] == []
    assert result["error"]["code"] == "API_NOT_CONFIGURED"


# ── Non-English interest behavioural test (1) ─────────────────────────────────


@pytest.mark.asyncio
async def test_non_english_interest_passes_through_without_crash():
    """Non-English interests (Hindi "खाना" = food) pass through to get_attractions
    without causing a crash or an empty-list short-circuit.

    Documented behaviour (Phase 8):
    - get_attractions_node does NOT filter or validate interest language.
    - The MCP tool embeds interests verbatim in its Google Maps query string.
    - Results may degrade (fewer results) vs. an English term, but the system
      remains alive and returns whatever Google Maps found.
    - The intent_parsing_node may translate "खाना" → "food" on its own run,
      but once interests are already in state the node skips the LLM call,
      so the raw Hindi term survives to the search node in this test.
    """
    hindi_state: ActivitiesState = {
        "destination": "Goa",
        "interests": ["खाना"],   # Hindi for "food" — non-English interest
        "limit": 5,
    }
    mock_call = AsyncMock(return_value=[_sample_attraction_dict()])
    with patch("src.ai.agents.activities_agent.call_tool", mock_call):
        result = await get_attractions_node(hindi_state)

    # The node called the tool — no crash, no silent skip
    mock_call.assert_awaited_once()
    called_interests = mock_call.await_args.args[1]["interests"]
    assert "खाना" in called_interests

    # System remains functional — partial result returned, no exception raised
    assert result["error"] is None

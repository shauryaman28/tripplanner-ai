"""
Unit tests for Phase 7 — FlightAgent router, clarify_node, and intent_parsing_node.

8 tests total — all deterministic, LLM mocked where needed.
Tests the router() conditional edge, clarify_node() question selection,
and intent_parsing_node() pass-through behavior.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.agents.flight_agent import (
    TripState,
    clarify_node,
    intent_parsing_node,
    router,
)


# ── Router tests (5) ──────────────────────────────────────────────────────

FULL_STATE: TripState = {
    "destination": "GOI",
    "origin": "DEL",
    "date": "2026-12-10",
    "budget": 20000,
    "passengers": 1,
}


def test_router_all_required_fields_present_returns_search():
    """All required fields (destination, date, budget) present → 'search'."""
    assert router(FULL_STATE) == "search"


def test_router_missing_destination_returns_clarify():
    """destination is None → 'clarify'."""
    state = {**FULL_STATE, "destination": None}
    assert router(state) == "clarify"


def test_router_missing_date_returns_clarify():
    """date is None → 'clarify'."""
    state = {**FULL_STATE, "date": None}
    assert router(state) == "clarify"


def test_router_missing_budget_returns_clarify():
    """budget is None → 'clarify'."""
    state = {**FULL_STATE, "budget": None}
    assert router(state) == "clarify"


def test_router_optional_fields_missing_still_returns_search():
    """origin and passengers are optional — missing them should still → 'search'."""
    state: TripState = {
        "destination": "GOI",
        "date": "2026-12-10",
        "budget": 20000,
    }
    assert router(state) == "search"


# ── clarify_node tests (2) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clarify_node_missing_destination_asks_where():
    """First missing field is destination → asks 'Where would you like to fly to?'"""
    state: TripState = {"date": "2026-12-10", "budget": 20000}
    result = await clarify_node(state)

    assert result["clarification_question"] == "Where would you like to fly to?"
    assert result["flights"] == []
    assert result["error"] is None


@pytest.mark.asyncio
async def test_clarify_node_missing_date_asks_when():
    """destination present but date missing → asks about date."""
    state: TripState = {"destination": "GOI", "budget": 20000}
    result = await clarify_node(state)

    assert result["clarification_question"] == "What date would you like to travel?"


# ── intent_parsing_node tests (1) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_intent_parsing_no_raw_input_passes_through():
    """No raw_input in state → node returns state unchanged, no LLM call made."""
    state: TripState = {
        "destination": "GOI",
        "origin": "DEL",
        "date": "2026-12-10",
        "budget": 20000,
        "passengers": 1,
    }
    with patch("src.ai.agents.flight_agent.ChatGoogleGenerativeAI") as MockLLM:
        result = await intent_parsing_node(state)

    # LLM was never instantiated
    MockLLM.assert_not_called()
    # State passed through unchanged
    assert result == state

"""
Unit tests for Phase 12 ItineraryBuilder.

All tests patch `_call_claude_llm` — the single seam between this module
and the Anthropic SDK — so nothing here makes a network call.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.ai.builder.builder import (
    BuilderError,
    ItineraryBuilder,
    ItineraryDraft,
    build_itinerary,
)

TRIP_META = {"destination": "Goa", "start_date": "2026-12-10", "end_date": "2026-12-11", "group_size": 2}

FLIGHTS = [{"airline": "6E", "flight_number": "6E-204", "price_inr": 8200.0}]
HOTELS = [{"name": "Goa Grand", "price_per_night_inr": 4500.0}]
ATTRACTIONS = [{"name": "Fort Aguada", "category": "history"}]


def _good_draft_json() -> str:
    return json.dumps({
        "days": [
            {
                "day": 1,
                "date": "2026-12-10",
                "morning": {"activity": "Fort Aguada", "cost": 0, "lat": 15.5, "lng": 73.7},
                "afternoon": None,
                "evening": None,
                "hotel": {"name": "Goa Grand", "cost_per_night": 4500.0},
                "flight": None,
            }
        ],
        "total_cost": 8200.0 + 4500.0,
        "currency": "INR",
    })


@pytest.mark.asyncio
async def test_build_itinerary_valid_output_matches_schema():
    with patch("src.ai.builder.builder._call_claude_llm", AsyncMock(return_value=_good_draft_json())):
        result = await build_itinerary(TRIP_META, FLIGHTS, HOTELS, ATTRACTIONS)

    assert isinstance(result, ItineraryDraft)
    assert result.total_cost == 12700.0
    assert result.days[0].hotel.name == "Goa Grand"


@pytest.mark.asyncio
async def test_build_itinerary_rejects_hallucinated_activity():
    bad = json.loads(_good_draft_json())
    bad["days"][0]["morning"]["activity"] = "Made Up Museum"  # not in ATTRACTIONS
    with patch("src.ai.builder.builder._call_claude_llm", AsyncMock(return_value=json.dumps(bad))):
        result = await build_itinerary(TRIP_META, FLIGHTS, HOTELS, ATTRACTIONS)

    assert isinstance(result, BuilderError)
    assert result.code == "DATA_SCOPE_VIOLATION"


@pytest.mark.asyncio
async def test_build_itinerary_budget_math_inconsistent():
    bad = json.loads(_good_draft_json())
    bad["total_cost"] = 999_999.0  # wildly off from sum of parts
    with patch("src.ai.builder.builder._call_claude_llm", AsyncMock(return_value=json.dumps(bad))):
        result = await build_itinerary(TRIP_META, FLIGHTS, HOTELS, ATTRACTIONS)

    assert isinstance(result, BuilderError)
    assert result.code == "BUDGET_MATH_INCONSISTENT"


@pytest.mark.asyncio
async def test_build_itinerary_llm_error_returns_builder_error():
    with patch("src.ai.builder.builder._call_claude_llm", AsyncMock(side_effect=Exception("timeout"))):
        result = await build_itinerary(TRIP_META, FLIGHTS, HOTELS, ATTRACTIONS)

    assert isinstance(result, BuilderError)
    assert result.code == "LLM_ERROR"


@pytest.mark.asyncio
async def test_build_itinerary_invalid_json_returns_parse_error():
    with patch("src.ai.builder.builder._call_claude_llm", AsyncMock(return_value="not json at all")):
        result = await build_itinerary(TRIP_META, FLIGHTS, HOTELS, ATTRACTIONS)

    assert isinstance(result, BuilderError)
    assert result.code == "JSON_PARSE_ERROR"


@pytest.mark.asyncio
async def test_itinerary_builder_run_logs_agent_run():
    mock_session = AsyncMock()
    added = []
    mock_session.add = lambda obj: added.append(obj)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()

    with patch("src.ai.builder.builder._call_claude_llm", AsyncMock(return_value=_good_draft_json())):
        builder = ItineraryBuilder()
        result = await builder.run(
            TRIP_META, FLIGHTS, HOTELS, ATTRACTIONS, db=mock_session, trip_id=uuid.uuid4()
        )

    assert result["error"] is None
    assert result["draft"]["total_cost"] == 12700.0
    assert len(added) == 1
    assert added[0].agent_name == "itinerary_builder"
    assert added[0].status == "completed"

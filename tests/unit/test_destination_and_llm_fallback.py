"""Regressions from the first hands-on run of an out-of-scope trip ("London", 365 nights).

What happened, and what these tests pin down:

  place lookup   "London" searched inside India is "The London Bridge", a road in Pune — the trip
                 was planned around Pune. A destination abroad is now reported as such.
  orchestrator   with nothing found the run saved an empty ₹0 itinerary as "planned". It now fails
                 and says why.
  evaluator      a 366-day trip came back as one day and passed. Every day must be planned.
  trip routes    a year-long trip could be created at all.
  LLM calls      Gemini's free tier ran out (20 requests a day); each call then spent ~36 s in
                 retries, which timed out POST /refine. One attempt, then another provider.
"""

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.schemas.trip import MAX_TRIP_NIGHTS, TripCreate
from src.ai import llm
from src.ai.builder.builder import _build_user_prompt
from src.ai.mcp_server.models import AttractionInput, HotelSearchInput
from src.ai.mcp_server.tools import OutsideCoverage, _geocode, _lookup_place, get_attractions, search_hotels
from src.ai.orchestrator.orchestrator import OrchestratorAgent

START = date.today() + timedelta(days=30)


# ── Place lookup ───────────────────────────────────────────────────────────


def _match(cls: str, importance: float, country: str = "in", lat: float = 1.0, name: str = "India") -> dict:
    """One Nominatim result."""
    return {
        "lat": str(lat),
        "lon": "2.0",
        "class": cls,
        "importance": importance,
        "address": {"country_code": country, "country": name},
    }


def _lookup(in_country: list[dict], worldwide: list[dict] | None = None) -> tuple[dict, MagicMock]:
    """Run _lookup_place against canned Nominatim answers; returns (result, the mock)."""

    def answer(_query: str, country_code: str | None = None) -> list[dict]:
        return in_country if country_code else (worldwide or [])

    with patch("src.ai.mcp_server.tools._nominatim", side_effect=answer) as nominatim:
        return _lookup_place("Somewhere"), nominatim


def test_a_well_known_place_at_home_needs_one_request():
    result, nominatim = _lookup([_match("boundary", 0.65, lat=15.3)])
    assert result == {"lat": 15.3, "lon": 2.0}
    assert nominatim.call_count == 1


def test_a_road_that_shares_a_foreign_citys_name_is_not_the_destination():
    """ "London" in India → "The London Bridge", Pune. The real London is abroad."""
    result, _ = _lookup(
        in_country=[_match("highway", 0.05)],
        worldwide=[_match("boundary", 0.89, country="gb", name="United Kingdom")],
    )
    assert result == {"outside": "United Kingdom"}


def test_a_minor_namesake_at_home_loses_to_a_famous_place_abroad():
    """ "Bali" is a town in Rajasthan (importance 0.16) and an island in Indonesia (0.65)."""
    result, _ = _lookup(
        in_country=[_match("place", 0.16)], worldwide=[_match("boundary", 0.65, country="id", name="Indonesia")]
    )
    assert result == {"outside": "Indonesia"}


def test_a_small_place_at_home_is_kept_when_nothing_abroad_outranks_it():
    hampi = _match("place", 0.26, lat=15.33)
    assert _lookup([hampi], worldwide=[hampi])[0] == {"lat": 15.33, "lon": 2.0}
    # a namesake abroad that is no better known does not win either
    assert _lookup([hampi], worldwide=[_match("place", 0.3, country="np", name="Nepal")])[0] == {
        "lat": 15.33,
        "lon": 2.0,
    }


def test_a_settlement_is_preferred_to_whatever_ranks_first():
    """ "Andaman" → a wood ranks first; the union territory is the destination."""
    result, _ = _lookup([_match("natural", 0.11, lat=9.0), _match("boundary", 0.59, lat=12.0)])
    assert result["lat"] == 12.0


def test_a_name_only_a_landmark_answers_to_is_still_found():
    """ "Alleppey" has no settlement entry under that spelling — the station is the only match anywhere."""
    assert _lookup([_match("railway", 0.3, lat=9.49)])[0] == {"lat": 9.49, "lon": 2.0}


def test_an_unknown_name_is_unknown():
    assert _lookup([])[0] == {}


def test_geocode_caches_every_outcome_including_abroad():
    stored: dict[str, dict] = {}
    with (
        patch("src.ai.mcp_server.tools.get_cached_sync", side_effect=stored.get),
        patch(
            "src.ai.mcp_server.tools.set_cached_sync", side_effect=lambda key, value, _ttl: stored.update({key: value})
        ),
        patch("src.ai.mcp_server.tools._lookup_place", return_value={"outside": "United Kingdom"}) as lookup,
    ):
        for _ in range(2):
            with pytest.raises(OutsideCoverage, match="London is in United Kingdom"):
                _geocode("London")

    assert lookup.call_count == 1  # the second call was answered from the cache
    assert all(key.startswith("mcp:geocode:v2:") for key in stored)  # entries cached by the old lookup are not reused


def _settings() -> MagicMock:
    return MagicMock(OPENTRIPMAP_API_KEY="k", LITEAPI_API_KEY="k")


def test_attractions_and_hotels_report_a_destination_abroad():
    no_hotels = MagicMock()
    no_hotels.json.return_value = {"hotels": [], "data": []}
    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools._geocode", side_effect=OutsideCoverage("London", "United Kingdom")),
        patch("src.ai.mcp_server.tools.httpx.post", return_value=no_hotels),
    ):
        attractions = get_attractions(AttractionInput(destination="London", interests=["history"], limit=5))
        hotels = search_hotels(
            HotelSearchInput(
                destination="London",
                check_in=str(START),
                check_out=str(START + timedelta(days=2)),
                budget_per_night=5_000,
                guests=2,
            )
        )

    for result in (attractions, hotels):
        assert result.code == "OUTSIDE_COVERAGE"
        assert result.error == "London is in United Kingdom. This planner covers trips within India for now."


# ── Orchestrator: nothing found ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_run_that_finds_nothing_fails_with_the_reason_instead_of_saving_an_empty_plan():
    abroad = {
        "error": "London is in United Kingdom. This planner covers trips within India for now.",
        "code": "OUTSIDE_COVERAGE",
    }
    published = []

    async def publish(event):
        published.append(event)

    with (
        patch("src.ai.orchestrator.orchestrator.FlightAgent") as flights,
        patch("src.ai.orchestrator.orchestrator.HotelAgent") as hotels,
        patch("src.ai.orchestrator.orchestrator.ActivitiesAgent") as activities,
        patch("src.ai.orchestrator.orchestrator.ItineraryBuilder") as builder,
    ):
        flights.return_value.run = AsyncMock(
            return_value={
                "flights": [],
                "error": {"error": "Unknown airport: 'London'.", "code": "UNKNOWN_DESTINATION"},
            }
        )
        hotels.return_value.run = AsyncMock(return_value={"hotels": [], "error": abroad})
        activities.return_value.run = AsyncMock(return_value={"attractions": [], "error": abroad})
        builder.return_value.run = AsyncMock()

        result = await OrchestratorAgent().run(
            {
                "destination": "London",
                "start_date": str(START),
                "end_date": str(START + timedelta(days=3)),
                "budget": 50_000.0,
                "group_size": 2,
                "interests": ["history"],
            },
            publish_fn=publish,
        )

    builder.return_value.run.assert_not_awaited()  # no retries over data that will never arrive
    assert result["itinerary_id"] is None
    assert published[-1] == {
        "event": "planning_failed",
        "agent": "orchestrator",
        "status": "failed",
        "error": abroad["error"],
    }


# ── Builder prompt and trip length ─────────────────────────────────────────


def test_the_builder_is_told_exactly_how_many_days_to_write():
    prompt = _build_user_prompt(
        {"destination": "Goa", "start_date": "2026-12-10", "end_date": "2026-12-14"}, [], [], []
    )
    assert '"days" must have exactly 5 entries' in prompt and "from 2026-12-10 to 2026-12-14 inclusive" in prompt


def test_a_trip_can_be_at_most_two_weeks_long():
    def trip(nights: int) -> TripCreate:
        return TripCreate(destination="Goa", start_date=START, end_date=START + timedelta(days=nights), budget=50_000)

    assert trip(MAX_TRIP_NIGHTS).end_date == START + timedelta(days=14)
    for nights in (15, 365):
        with pytest.raises(ValidationError, match="at most 14 nights"):
            trip(nights)


# ── Short LLM calls: one attempt, then another provider ───────────────────


def _gemini(reply: str | Exception) -> MagicMock:
    client = MagicMock()
    client.return_value.ainvoke = AsyncMock(
        side_effect=reply if isinstance(reply, Exception) else None, return_value=MagicMock(content=reply)
    )
    return client


@pytest.mark.asyncio
async def test_gemini_answers_without_retries_when_it_can(no_fallback_llm):
    with patch("src.ai.llm.ChatGoogleGenerativeAI", _gemini('{"ok": true}')) as gemini:
        assert await llm.ask("prompt") == '{"ok": true}'

    assert gemini.call_args.kwargs["max_retries"] == 1  # the SDK default spends ~36 s on a quota error
    assert gemini.call_args.kwargs["timeout"] == llm.SHORT_PROMPT_TIMEOUT_S
    no_fallback_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_groq_answers_when_gemini_cannot():
    with (
        patch("src.ai.llm.ChatGoogleGenerativeAI", _gemini(RuntimeError("429 RESOURCE_EXHAUSTED"))),
        patch("src.ai.llm.settings", MagicMock(GROQ_API_KEY="key")),
        patch("src.ai.llm._ask_groq", AsyncMock(return_value='{"from": "groq"}')) as groq,
    ):
        assert await llm.ask("prompt") == '{"from": "groq"}'
    groq.assert_awaited_once_with("prompt")


@pytest.mark.asyncio
async def test_without_a_second_provider_the_gemini_error_is_raised(no_fallback_llm):
    with (
        patch("src.ai.llm.ChatGoogleGenerativeAI", _gemini(RuntimeError("429 RESOURCE_EXHAUSTED"))),
        patch("src.ai.llm.settings", MagicMock(GROQ_API_KEY="")),
        pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"),
    ):
        await llm.ask("prompt")
    no_fallback_llm.assert_not_awaited()

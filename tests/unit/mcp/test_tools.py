"""
Unit tests for Phase 2 MCP tools.
Pure functions — no server, no network, no Docker.

Coverage:
  - Valid input → expected output shape
  - Invalid input → expected ToolError shape + correct error code
"""

import pytest
from datetime import date, timedelta

from src.ai.mcp_server.models import Flight, Hotel, Attraction, DayForecast, BudgetEstimate, ToolError
from src.ai.mcp_server.tools import (
    estimate_budget,
    get_attractions,
    get_weather,
    search_flights,
    search_hotels,
)
from src.ai.mcp_server.models import (
    AttractionInput,
    BudgetInput,
    FlightSearchInput,
    HotelSearchInput,
    WeatherInput,
)


FUTURE = (date.today() + timedelta(days=30)).isoformat()
PAST = (date.today() - timedelta(days=1)).isoformat()


# ── search_flights ─────────────────────────────────────────────────────────────

def test_search_flights_valid():
    result = search_flights(FlightSearchInput(
        origin="DEL", destination="GOI", date=FUTURE, budget=20_000, passengers=1
    ))
    assert isinstance(result, list)
    assert len(result) > 0
    assert all(isinstance(f, Flight) for f in result)
    assert all(f.price_inr > 0 for f in result)


def test_search_flights_past_date():
    result = search_flights(FlightSearchInput(
        origin="DEL", destination="GOI", date=PAST, budget=20_000, passengers=1
    ))
    assert isinstance(result, ToolError)
    assert result.code == "PAST_DATE"


def test_search_flights_budget_too_low():
    result = search_flights(FlightSearchInput(
        origin="DEL", destination="GOI", date=FUTURE, budget=500, passengers=1
    ))
    assert isinstance(result, ToolError)
    assert result.code == "BUDGET_TOO_LOW"


def test_search_flights_scales_with_passengers():
    r1 = search_flights(FlightSearchInput(origin="DEL", destination="GOI", date=FUTURE, budget=50_000, passengers=1))
    r2 = search_flights(FlightSearchInput(origin="DEL", destination="GOI", date=FUTURE, budget=50_000, passengers=2))
    assert isinstance(r1, list) and isinstance(r2, list)
    assert r2[0].price_inr == r1[0].price_inr * 2


# ── search_hotels ──────────────────────────────────────────────────────────────

def test_search_hotels_valid():
    result = search_hotels(HotelSearchInput(
        destination="Goa", check_in="2025-12-10", check_out="2025-12-15",
        budget_per_night=5_000, guests=2
    ))
    assert isinstance(result, list)
    assert all(isinstance(h, Hotel) for h in result)


def test_search_hotels_invalid_dates():
    result = search_hotels(HotelSearchInput(
        destination="Goa", check_in="2025-12-15", check_out="2025-12-10",
        budget_per_night=5_000, guests=1
    ))
    assert isinstance(result, ToolError)
    assert result.code == "INVALID_DATES"


def test_search_hotels_respects_budget():
    budget = 2_000.0
    result = search_hotels(HotelSearchInput(
        destination="Goa", check_in="2025-12-10", check_out="2025-12-15",
        budget_per_night=budget, guests=1
    ))
    assert isinstance(result, list)
    assert all(h.price_per_night_inr <= budget for h in result)


# ── get_attractions ────────────────────────────────────────────────────────────

def test_get_attractions_valid():
    result = get_attractions(AttractionInput(destination="Goa", interests=["history"], limit=3))
    assert isinstance(result, list)
    assert len(result) <= 3
    assert all(isinstance(a, Attraction) for a in result)


def test_get_attractions_limit_exceeded():
    result = get_attractions(AttractionInput(destination="Goa", interests=[], limit=15))
    assert isinstance(result, ToolError)
    assert result.code == "LIMIT_EXCEEDED"


def test_get_attractions_no_matching_interests_falls_back():
    result = get_attractions(AttractionInput(destination="Goa", interests=["nonexistent"], limit=5))
    assert isinstance(result, list)
    assert len(result) > 0


# ── get_weather ────────────────────────────────────────────────────────────────

def test_get_weather_valid():
    result = get_weather(WeatherInput(destination="Goa", date_range="2025-12-10 to 2025-12-17"))
    assert isinstance(result, list)
    assert len(result) == 7
    assert all(isinstance(d, DayForecast) for d in result)


def test_get_weather_empty_destination():
    result = get_weather(WeatherInput(destination="  ", date_range="2025-12-10 to 2025-12-17"))
    assert isinstance(result, ToolError)
    assert result.code == "MISSING_DESTINATION"


# ── estimate_budget ────────────────────────────────────────────────────────────

def test_estimate_budget_sums_correctly():
    result = estimate_budget(BudgetInput(flights=8_000, hotels=3_000, days=5, daily_spend=2_000))
    assert isinstance(result, BudgetEstimate)
    assert result.total == 8_000 + (3_000 * 5) + (2_000 * 5)


def test_estimate_budget_high_budget_note():
    result = estimate_budget(BudgetInput(flights=50_000, hotels=10_000, days=7, daily_spend=5_000))
    assert "₹80,000" in result.notes

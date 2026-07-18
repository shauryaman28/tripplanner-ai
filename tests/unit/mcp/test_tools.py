"""
Unit tests for Phase 3 MCP tools.

Strategy:
- Validation tests (date, budget, limit, destination):
    Return *before* any API call — no mocking needed. ✓
- Missing-key tests:
    Return API_NOT_CONFIGURED before any network call. ✓
- Happy-path tests:
    Patch the external SDK clients so tests run with zero network. ✓
- estimate_budget:
    Pure arithmetic — no mocking ever needed. ✓
"""

import os
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.ai.mcp_server.models import (
    Attraction,
    AttractionInput,
    BudgetEstimate,
    BudgetInput,
    DayForecast,
    Flight,
    FlightSearchInput,
    Hotel,
    HotelSearchInput,
    ToolError,
    WeatherInput,
)
from src.ai.mcp_server.tools import (
    estimate_budget,
    get_attractions,
    get_weather,
    search_flights,
    search_hotels,
)

FUTURE = (date.today() + timedelta(days=30)).isoformat()
PAST = (date.today() - timedelta(days=1)).isoformat()


# ── Helpers ────────────────────────────────────────────────────────────────


def _fake_settings(client_id="fake_id", secret="fake_secret", gmaps="fake_key", owm="fake_key"):
    """Return a MagicMock that looks like a configured mcp_settings."""
    s = MagicMock()
    s.AMADEUS_CLIENT_ID = client_id
    s.AMADEUS_CLIENT_SECRET = secret
    s.GOOGLE_MAPS_API_KEY = gmaps
    s.OPENWEATHER_API_KEY = owm
    return s


# ── search_flights ─────────────────────────────────────────────────────────


def test_search_flights_past_date():
    result = search_flights(
        FlightSearchInput(origin="DEL", destination="GOI", date=PAST, budget=20_000, passengers=1)
    )
    assert isinstance(result, ToolError)
    assert result.code == "PAST_DATE"


def test_search_flights_budget_too_low():
    result = search_flights(
        FlightSearchInput(origin="DEL", destination="GOI", date=FUTURE, budget=500, passengers=1)
    )
    assert isinstance(result, ToolError)
    assert result.code == "BUDGET_TOO_LOW"


def test_search_flights_no_api_key():
    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings(client_id="", secret="")):
        result = search_flights(
            FlightSearchInput(origin="DEL", destination="GOI", date=FUTURE, budget=20_000, passengers=1)
        )
    assert isinstance(result, ToolError)
    assert result.code == "API_NOT_CONFIGURED"


def test_search_flights_valid():
    """Happy path — Amadeus SDK mocked to return one flight offer."""
    mock_response = MagicMock()
    mock_response.data = [
        {
            "itineraries": [
                {
                    "segments": [
                        {
                            "carrierCode": "6E",
                            "number": "204",
                            "departure": {"at": f"{FUTURE}T06:00:00"},
                            "arrival": {"at": f"{FUTURE}T08:15:00"},
                        }
                    ],
                    "duration": "PT2H15M",
                }
            ],
            "price": {"grandTotal": "4200.00"},
        }
    ]

    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()), \
         patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None), \
         patch("src.ai.mcp_server.tools.set_cached_sync"), \
         patch("src.ai.mcp_server.tools.AmadeusClient") as MockClient:

        MockClient.return_value.shopping.flight_offers_search.get.return_value = mock_response
        result = search_flights(
            FlightSearchInput(origin="DEL", destination="GOI", date=FUTURE, budget=20_000, passengers=1)
        )

    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], Flight)
    assert result[0].airline == "6E"
    assert result[0].price_inr == 4200.0
    assert result[0].duration_mins == 135


def test_search_flights_scales_with_passengers():
    """price_inr must be multiplied by passenger count."""
    mock_response = MagicMock()
    mock_response.data = [
        {
            "itineraries": [
                {
                    "segments": [
                        {
                            "carrierCode": "6E",
                            "number": "204",
                            "departure": {"at": f"{FUTURE}T06:00:00"},
                            "arrival": {"at": f"{FUTURE}T08:15:00"},
                        }
                    ],
                    "duration": "PT2H15M",
                }
            ],
            "price": {"grandTotal": "4200.00"},
        }
    ]

    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()), \
         patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None), \
         patch("src.ai.mcp_server.tools.set_cached_sync"), \
         patch("src.ai.mcp_server.tools.AmadeusClient") as MockClient:

        MockClient.return_value.shopping.flight_offers_search.get.return_value = mock_response

        r1 = search_flights(
            FlightSearchInput(origin="DEL", destination="GOI", date=FUTURE, budget=50_000, passengers=1)
        )
        r2 = search_flights(
            FlightSearchInput(origin="DEL", destination="GOI", date=FUTURE, budget=50_000, passengers=2)
        )

    assert isinstance(r1, list) and isinstance(r2, list)
    assert r2[0].price_inr == r1[0].price_inr * 2


# ── search_hotels ──────────────────────────────────────────────────────────


def test_search_hotels_invalid_dates():
    result = search_hotels(
        HotelSearchInput(
            destination="Goa", check_in="2025-12-15", check_out="2025-12-10",
            budget_per_night=5_000, guests=1,
        )
    )
    assert isinstance(result, ToolError)
    assert result.code == "INVALID_DATES"


def test_search_hotels_no_api_key():
    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings(client_id="", secret="")):
        result = search_hotels(
            HotelSearchInput(
                destination="Goa", check_in="2025-12-10", check_out="2025-12-15",
                budget_per_night=5_000, guests=1,
            )
        )
    assert isinstance(result, ToolError)
    assert result.code == "API_NOT_CONFIGURED"


def test_search_hotels_unknown_destination():
    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()):
        result = search_hotels(
            HotelSearchInput(
                destination="Atlantis", check_in="2025-12-10", check_out="2025-12-15",
                budget_per_night=5_000, guests=1,
            )
        )
    assert isinstance(result, ToolError)
    assert result.code == "UNKNOWN_DESTINATION"


def test_search_hotels_valid():
    """Happy path — Amadeus hotel search mocked."""
    hotels_list_resp = MagicMock()
    hotels_list_resp.data = [{"hotelId": "HLGOAGOA"}]

    offers_resp = MagicMock()
    offers_resp.data = [
        {
            "hotel": {
                "name": "Goa Grand",
                "rating": "4",
                "address": {"lines": ["Beach Road"], "cityName": "Goa"},
            },
            "offers": [{"price": {"base": "4500.00"}}],
        }
    ]

    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()), \
         patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None), \
         patch("src.ai.mcp_server.tools.set_cached_sync"), \
         patch("src.ai.mcp_server.tools.AmadeusClient") as MockClient:

        client = MockClient.return_value
        client.reference_data.locations.hotels.by_city.get.return_value = hotels_list_resp
        client.shopping.hotel_offers_search.get.return_value = offers_resp

        result = search_hotels(
            HotelSearchInput(
                destination="Goa", check_in="2025-12-10", check_out="2025-12-15",
                budget_per_night=5_000, guests=2,
            )
        )

    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], Hotel)
    assert result[0].price_per_night_inr <= 5_000


def test_search_hotels_respects_budget():
    """Hotels over budget_per_night must be filtered out."""
    hotels_list_resp = MagicMock()
    hotels_list_resp.data = [{"hotelId": "H1"}, {"hotelId": "H2"}]

    offers_resp = MagicMock()
    offers_resp.data = [
        {
            "hotel": {"name": "Cheap", "rating": "3", "address": {"lines": [""], "cityName": "Goa"}},
            "offers": [{"price": {"base": "1500.00"}}],
        },
        {
            "hotel": {"name": "Expensive", "rating": "5", "address": {"lines": [""], "cityName": "Goa"}},
            "offers": [{"price": {"base": "9000.00"}}],  # over budget
        },
    ]

    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()), \
         patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None), \
         patch("src.ai.mcp_server.tools.set_cached_sync"), \
         patch("src.ai.mcp_server.tools.AmadeusClient") as MockClient:

        client = MockClient.return_value
        client.reference_data.locations.hotels.by_city.get.return_value = hotels_list_resp
        client.shopping.hotel_offers_search.get.return_value = offers_resp

        result = search_hotels(
            HotelSearchInput(
                destination="Goa", check_in="2025-12-10", check_out="2025-12-15",
                budget_per_night=2_000, guests=1,
            )
        )

    assert isinstance(result, list)
    assert all(h.price_per_night_inr <= 2_000 for h in result)


# ── get_attractions ────────────────────────────────────────────────────────


def test_get_attractions_limit_exceeded():
    result = get_attractions(AttractionInput(destination="Goa", interests=[], limit=15))
    assert isinstance(result, ToolError)
    assert result.code == "LIMIT_EXCEEDED"


def test_get_attractions_no_api_key():
    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings(gmaps="")):
        result = get_attractions(AttractionInput(destination="Goa", interests=[], limit=3))
    assert isinstance(result, ToolError)
    assert result.code == "API_NOT_CONFIGURED"


def test_get_attractions_valid():
    """Happy path — Google Maps mocked."""
    mock_places_result = {
        "results": [
            {
                "name": "Fort Aguada",
                "types": ["tourist_attraction"],
                "rating": 4.5,
                "geometry": {"location": {"lat": 15.5009, "lng": 73.7655}},
                "editorial_summary": {"overview": "17th-century fort."},
            }
        ]
    }

    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()), \
         patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None), \
         patch("src.ai.mcp_server.tools.set_cached_sync"), \
         patch("src.ai.mcp_server.tools.googlemaps") as mock_gm:

        mock_gm.Client.return_value.places.return_value = mock_places_result
        result = get_attractions(AttractionInput(destination="Goa", interests=["history"], limit=3))

    assert isinstance(result, list)
    assert len(result) <= 3
    assert all(isinstance(a, Attraction) for a in result)
    assert result[0].lat is not None  # lat/lng present (Phase 18 map)


def test_get_attractions_no_matching_interests_returns_results():
    """Even with niche interests, tool returns what Google Maps gives back."""
    mock_places_result = {
        "results": [
            {
                "name": "Some Place",
                "types": ["tourist_attraction"],
                "rating": 4.0,
                "geometry": {"location": {"lat": 15.0, "lng": 73.0}},
            }
        ]
    }

    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()), \
         patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None), \
         patch("src.ai.mcp_server.tools.set_cached_sync"), \
         patch("src.ai.mcp_server.tools.googlemaps") as mock_gm:

        mock_gm.Client.return_value.places.return_value = mock_places_result
        result = get_attractions(
            AttractionInput(destination="Goa", interests=["nonexistent_interest"], limit=5)
        )

    assert isinstance(result, list)
    assert len(result) > 0


# ── get_weather ────────────────────────────────────────────────────────────


def test_get_weather_empty_destination():
    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()):
        result = get_weather(WeatherInput(destination="  ", date_range="2025-12-10 to 2025-12-17"))
    assert isinstance(result, ToolError)
    assert result.code == "MISSING_DESTINATION"


def test_get_weather_no_api_key():
    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings(owm="")):
        result = get_weather(WeatherInput(destination="Goa", date_range="2025-12-10 to 2025-12-17"))
    assert isinstance(result, ToolError)
    assert result.code == "API_NOT_CONFIGURED"


def test_get_weather_invalid_date_range():
    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()):
        result = get_weather(WeatherInput(destination="Goa", date_range="not-a-date"))
    assert isinstance(result, ToolError)
    assert result.code == "INVALID_DATE_RANGE"


def test_get_weather_future_dates_returns_climate_estimate():
    """Dates >5 days away → climate estimate, no OWM call."""
    future_start = date.today() + timedelta(days=30)
    date_range = f"{future_start.isoformat()} to {(future_start + timedelta(days=6)).isoformat()}"

    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()), \
         patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None), \
         patch("src.ai.mcp_server.tools.set_cached_sync"):

        result = get_weather(WeatherInput(destination="Goa", date_range=date_range))

    assert isinstance(result, list)
    assert len(result) == 7
    assert all(isinstance(d, DayForecast) for d in result)
    assert all("climate estimate" in d.condition for d in result)


def test_get_weather_valid_near_term():
    """Near-future dates → real OWM call (mocked)."""
    tomorrow = date.today() + timedelta(days=1)
    date_range = f"{tomorrow.isoformat()} to {(tomorrow + timedelta(days=2)).isoformat()}"

    mock_owm_response = MagicMock()
    mock_owm_response.raise_for_status = MagicMock()
    mock_owm_response.json.return_value = {
        "list": [
            {
                "dt": int(tomorrow.strftime("%s") if hasattr(tomorrow, "strftime") else 1700000000),
                "weather": [{"main": "Sunny"}],
                "main": {"temp_max": 32.0, "temp_min": 24.0},
            }
        ]
    }

    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()), \
         patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None), \
         patch("src.ai.mcp_server.tools.set_cached_sync"), \
         patch("src.ai.mcp_server.tools.httpx") as mock_httpx:

        mock_httpx.get.return_value = mock_owm_response
        result = get_weather(WeatherInput(destination="Goa", date_range=date_range))

    assert isinstance(result, list)
    assert all(isinstance(d, DayForecast) for d in result)


# ── estimate_budget ────────────────────────────────────────────────────────


def test_estimate_budget_sums_correctly():
    result = estimate_budget(
        BudgetInput(flights=8_000, hotels=3_000, days=5, daily_spend=2_000)
    )
    assert isinstance(result, BudgetEstimate)
    assert result.total == 8_000 + (3_000 * 5) + (2_000 * 5)


def test_estimate_budget_high_budget_note():
    result = estimate_budget(
        BudgetInput(flights=50_000, hotels=10_000, days=7, daily_spend=5_000)
    )
    assert isinstance(result, BudgetEstimate)
    assert "₹80,000" in result.notes


def test_estimate_budget_negative_flights():
    result = estimate_budget(BudgetInput(flights=-1, hotels=3_000, days=5, daily_spend=1_000))
    assert isinstance(result, ToolError)
    assert result.code == "INVALID_INPUT"


def test_estimate_budget_negative_daily_spend():
    result = estimate_budget(BudgetInput(flights=5_000, hotels=2_000, days=3, daily_spend=-500))
    assert isinstance(result, ToolError)
    assert result.code == "INVALID_INPUT"

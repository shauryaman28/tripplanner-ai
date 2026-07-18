"""
Contract tests for Phase 3 MCP tools.

Verify response *shape* (field names, types) — not specific values.
These tests use mocked API responses so they run in CI without keys.

Real-API contract verification:
    RUN_INTEGRATION=1 pytest tests/integration/ -v
"""

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


def _settings(client_id="k", secret="s", gmaps="k", owm="k"):
    s = MagicMock()
    s.AMADEUS_CLIENT_ID = client_id
    s.AMADEUS_CLIENT_SECRET = secret
    s.GOOGLE_MAPS_API_KEY = gmaps
    s.OPENWEATHER_API_KEY = owm
    return s


# ── Flight contract ────────────────────────────────────────────────────────


class TestFlightContract:
    def test_error_has_code_and_error_fields(self):
        """ToolError shape: { error: str, code: str }."""
        with patch("src.ai.mcp_server.tools.mcp_settings", _settings(client_id="", secret="")):
            result = search_flights(
                FlightSearchInput(origin="DEL", destination="GOI", date=FUTURE, budget=20_000, passengers=1)
            )
        assert isinstance(result, ToolError)
        assert isinstance(result.code, str) and result.code
        assert isinstance(result.error, str) and result.error

    def test_flight_has_all_required_fields(self):
        """Flight shape: airline, flight_number, departure, arrival, duration_mins, price_inr, stops."""
        mock_resp = MagicMock()
        mock_resp.data = [{
            "itineraries": [{
                "segments": [{
                    "carrierCode": "6E", "number": "204",
                    "departure": {"at": f"{FUTURE}T06:00:00"},
                    "arrival": {"at": f"{FUTURE}T08:15:00"},
                }],
                "duration": "PT2H15M",
            }],
            "price": {"grandTotal": "4200.00"},
        }]

        with patch("src.ai.mcp_server.tools.mcp_settings", _settings()), \
             patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None), \
             patch("src.ai.mcp_server.tools.set_cached_sync"), \
             patch("src.ai.mcp_server.tools.AmadeusClient") as MC:
            MC.return_value.shopping.flight_offers_search.get.return_value = mock_resp
            result = search_flights(
                FlightSearchInput(origin="DEL", destination="GOI", date=FUTURE, budget=20_000, passengers=1)
            )

        assert isinstance(result, list)
        f = result[0]
        assert isinstance(f.airline, str)
        assert isinstance(f.flight_number, str)
        assert isinstance(f.departure, str)
        assert isinstance(f.arrival, str)
        assert isinstance(f.duration_mins, int)
        assert isinstance(f.price_inr, float)
        assert isinstance(f.stops, int)


# ── Hotel contract ─────────────────────────────────────────────────────────


class TestHotelContract:
    def test_hotel_has_all_required_fields(self):
        """Hotel shape: name, stars, price_per_night_inr, rating, address."""
        hotels_resp = MagicMock()
        hotels_resp.data = [{"hotelId": "H1"}]
        offers_resp = MagicMock()
        offers_resp.data = [{
            "hotel": {
                "name": "Test Hotel", "rating": "4",
                "address": {"lines": ["Road"], "cityName": "Goa"},
            },
            "offers": [{"price": {"base": "3000.00"}}],
        }]

        with patch("src.ai.mcp_server.tools.mcp_settings", _settings()), \
             patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None), \
             patch("src.ai.mcp_server.tools.set_cached_sync"), \
             patch("src.ai.mcp_server.tools.AmadeusClient") as MC:
            c = MC.return_value
            c.reference_data.locations.hotels.by_city.get.return_value = hotels_resp
            c.shopping.hotel_offers_search.get.return_value = offers_resp
            result = search_hotels(
                HotelSearchInput(destination="Goa", check_in="2025-12-10",
                                 check_out="2025-12-15", budget_per_night=5_000, guests=1)
            )

        assert isinstance(result, list)
        h = result[0]
        assert isinstance(h.name, str)
        assert isinstance(h.stars, int)
        assert isinstance(h.price_per_night_inr, float)
        assert isinstance(h.rating, float)
        assert isinstance(h.address, str)


# ── Attraction contract ────────────────────────────────────────────────────


class TestAttractionContract:
    def test_attraction_has_all_required_fields_including_lat_lng(self):
        """Attraction shape: name, category, rating, description, lat?, lng?."""
        mock_places = {"results": [{
            "name": "Fort Aguada",
            "types": ["tourist_attraction"],
            "rating": 4.5,
            "geometry": {"location": {"lat": 15.5, "lng": 73.7}},
            "editorial_summary": {"overview": "A fort."},
        }]}

        with patch("src.ai.mcp_server.tools.mcp_settings", _settings()), \
             patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None), \
             patch("src.ai.mcp_server.tools.set_cached_sync"), \
             patch("src.ai.mcp_server.tools.googlemaps") as mg:
            mg.Client.return_value.places.return_value = mock_places
            result = get_attractions(
                AttractionInput(destination="Goa", interests=["history"], limit=3)
            )

        assert isinstance(result, list)
        a = result[0]
        assert isinstance(a.name, str)
        assert isinstance(a.category, str)
        assert isinstance(a.rating, float)
        assert isinstance(a.description, str)
        assert a.lat is not None and isinstance(a.lat, float)
        assert a.lng is not None and isinstance(a.lng, float)


# ── Weather contract ───────────────────────────────────────────────────────


class TestWeatherContract:
    def test_forecast_has_all_required_fields(self):
        """DayForecast shape: date, condition, temp_high_c, temp_low_c."""
        future_start = date.today() + timedelta(days=30)
        date_range = (
            f"{future_start.isoformat()} to "
            f"{(future_start + timedelta(days=6)).isoformat()}"
        )

        with patch("src.ai.mcp_server.tools.mcp_settings", _settings()), \
             patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None), \
             patch("src.ai.mcp_server.tools.set_cached_sync"):
            result = get_weather(WeatherInput(destination="Goa", date_range=date_range))

        assert isinstance(result, list)
        assert len(result) == 7
        d = result[0]
        assert isinstance(d.date, str)
        assert isinstance(d.condition, str)
        assert isinstance(d.temp_high_c, float)
        assert isinstance(d.temp_low_c, float)
        assert d.temp_high_c >= d.temp_low_c


# ── Budget contract ────────────────────────────────────────────────────────


class TestBudgetContract:
    def test_budget_estimate_has_all_required_fields(self):
        """BudgetEstimate shape: flights, hotels, activities_estimate, total, per_person, notes."""
        result = estimate_budget(
            BudgetInput(flights=8_000, hotels=3_000, days=5, daily_spend=2_000)
        )
        assert isinstance(result, BudgetEstimate)
        assert isinstance(result.flights, float)
        assert isinstance(result.hotels, float)
        assert isinstance(result.activities_estimate, float)
        assert isinstance(result.total, float)
        assert isinstance(result.per_person, float)
        assert isinstance(result.notes, str)

    def test_total_equals_sum_of_parts(self):
        result = estimate_budget(
            BudgetInput(flights=5_000, hotels=2_000, days=3, daily_spend=1_500)
        )
        expected = 5_000 + (2_000 * 3) + (1_500 * 3)
        assert result.total == expected

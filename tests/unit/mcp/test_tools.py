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

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

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
from tests.fakes import duffel_offer, duffel_response

FUTURE = (date.today() + timedelta(days=30)).isoformat()
PAST = (date.today() - timedelta(days=1)).isoformat()


# ── Helpers ────────────────────────────────────────────────────────────────


def _fake_settings(client_id="fake_id", secret="fake_secret", otm="fake_key", owm="fake_key", duffel="fake_token"):
    """Return a MagicMock that looks like a configured mcp_settings."""
    s = MagicMock()
    s.DUFFEL_ACCESS_TOKEN = duffel
    s.AMADEUS_CLIENT_ID = client_id
    s.AMADEUS_CLIENT_SECRET = secret
    s.OPENTRIPMAP_API_KEY = otm
    s.OPENWEATHER_API_KEY = owm
    return s


# ── search_flights ─────────────────────────────────────────────────────────


def test_search_flights_past_date():
    result = search_flights(FlightSearchInput(origin="DEL", destination="GOI", date=PAST, budget=20_000, passengers=1))
    assert isinstance(result, ToolError)
    assert result.code == "PAST_DATE"


def test_search_flights_budget_too_low():
    result = search_flights(FlightSearchInput(origin="DEL", destination="GOI", date=FUTURE, budget=500, passengers=1))
    assert isinstance(result, ToolError)
    assert result.code == "BUDGET_TOO_LOW"


def test_search_flights_no_api_key():
    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings(duffel="")):
        result = search_flights(
            FlightSearchInput(origin="DEL", destination="GOI", date=FUTURE, budget=20_000, passengers=1)
        )
    assert isinstance(result, ToolError)
    assert result.code == "API_NOT_CONFIGURED"


def test_search_flights_malformed_date_is_a_tool_error():
    result = search_flights(FlightSearchInput(origin="DEL", destination="GOI", date="next friday", budget=20_000))
    assert isinstance(result, ToolError)
    assert result.code == "INVALID_DATES"


def test_search_flights_unknown_city_is_a_tool_error():
    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()):
        result = search_flights(FlightSearchInput(origin="DEL", destination="Atlantis", date=FUTURE, budget=20_000))
    assert isinstance(result, ToolError)
    assert result.code == "UNKNOWN_DESTINATION"


def _search_flights(*offers, **input_overrides):
    """Run search_flights against a faked Duffel response; returns (result, httpx.post mock)."""
    params = {"origin": "DEL", "destination": "GOI", "date": FUTURE, "budget": 20_000, "passengers": 1}
    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools.httpx.post", return_value=duffel_response(*offers)) as mock_post,
    ):
        return search_flights(FlightSearchInput(**{**params, **input_overrides})), mock_post


def test_search_flights_valid():
    """Happy path — Duffel mocked to return one offer."""
    result, _ = _search_flights(duffel_offer(day=FUTURE))

    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], Flight)
    assert result[0].airline == "6E"
    assert result[0].flight_number == "6E-204"
    assert result[0].price_inr == 4200.0
    assert result[0].duration_mins == 135
    assert result[0].stops == 0


def test_search_flights_resolves_city_names_and_sends_one_passenger_per_traveller():
    """Agents pass city names ("Goa"); Duffel needs IATA codes and one passenger entry each."""
    _, mock_post = _search_flights(duffel_offer(), origin="Delhi", destination="Goa", passengers=2)

    body = mock_post.call_args.kwargs["json"]["data"]
    assert body["slices"] == [{"origin": "DEL", "destination": "GOI", "departure_date": FUTURE}]
    assert len(body["passengers"]) == 2


def test_search_flights_round_trip_adds_return_slice():
    return_date = (date.today() + timedelta(days=35)).isoformat()
    _, mock_post = _search_flights(duffel_offer(), return_date=return_date)

    slices = mock_post.call_args.kwargs["json"]["data"]["slices"]
    assert slices[1] == {"origin": "GOI", "destination": "DEL", "departure_date": return_date}


def test_search_flights_sorts_by_price_and_enforces_budget_cap():
    """The budget is a real cap — it is what makes the Phase 10 re-plan loop meaningful."""
    result, _ = _search_flights(
        duffel_offer("AI", "805", "9000.00"),
        duffel_offer("6E", "204", "4200.00"),
        duffel_offer("UK", "995", "25000.00"),  # over the ₹20,000 cap → dropped
    )
    assert [f.airline for f in result] == ["6E", "AI"]


def test_search_flights_nothing_within_budget_is_no_results():
    result, _ = _search_flights(duffel_offer(amount="25000.00"))
    assert isinstance(result, ToolError)
    assert result.code == "NO_RESULTS"


def test_search_flights_converts_currency_and_skips_unknown_ones():
    result, _ = _search_flights(
        duffel_offer("BA", "1", "100.00", currency="GBP"),
        duffel_offer("XX", "2", "100.00", currency="XYZ"),
    )
    assert [f.airline for f in result] == ["BA"]
    assert result[0].price_inr == 10_700.0


# ── search_hotels ──────────────────────────────────────────────────────────


def test_search_hotels_invalid_dates():
    result = search_hotels(
        HotelSearchInput(
            destination="Goa",
            check_in="2025-12-15",
            check_out="2025-12-10",
            budget_per_night=5_000,
            guests=1,
        )
    )
    assert isinstance(result, ToolError)
    assert result.code == "INVALID_DATES"


def test_search_hotels_no_api_key():
    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings(client_id="", secret="")):
        result = search_hotels(
            HotelSearchInput(
                destination="Goa",
                check_in="2025-12-10",
                check_out="2025-12-15",
                budget_per_night=5_000,
                guests=1,
            )
        )
    assert isinstance(result, ToolError)
    assert result.code == "API_NOT_CONFIGURED"


def test_search_hotels_unknown_destination():
    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()):
        result = search_hotels(
            HotelSearchInput(
                destination="Atlantis",
                check_in="2025-12-10",
                check_out="2025-12-15",
                budget_per_night=5_000,
                guests=1,
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

    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools.AmadeusClient") as MockClient,
    ):
        client = MockClient.return_value
        client.reference_data.locations.hotels.by_city.get.return_value = hotels_list_resp
        client.shopping.hotel_offers_search.get.return_value = offers_resp

        result = search_hotels(
            HotelSearchInput(
                destination="Goa",
                check_in="2025-12-10",
                check_out="2025-12-15",
                budget_per_night=5_000,
                guests=2,
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

    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools.AmadeusClient") as MockClient,
    ):
        client = MockClient.return_value
        client.reference_data.locations.hotels.by_city.get.return_value = hotels_list_resp
        client.shopping.hotel_offers_search.get.return_value = offers_resp

        result = search_hotels(
            HotelSearchInput(
                destination="Goa",
                check_in="2025-12-10",
                check_out="2025-12-15",
                budget_per_night=2_000,
                guests=1,
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
    with patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings(otm="")):
        result = get_attractions(AttractionInput(destination="Goa", interests=[], limit=3))
    assert isinstance(result, ToolError)
    assert result.code == "API_NOT_CONFIGURED"


def test_get_attractions_valid():
    """Happy path — OpenTripMap geocode + radius search mocked via httpx."""
    geo_response = MagicMock()
    geo_response.raise_for_status = MagicMock()
    geo_response.json.return_value = {"lat": 15.4909, "lon": 73.8278}

    radius_response = MagicMock()
    radius_response.raise_for_status = MagicMock()
    radius_response.json.return_value = [
        {
            "name": "Fort Aguada",
            "kinds": "historic,fortifications,tourist_facilities",
            "rate": 4,
            "point": {"lat": 15.5009, "lon": 73.7655},
        }
    ]

    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools.httpx") as mock_httpx,
    ):
        mock_httpx.get.side_effect = [geo_response, radius_response]
        result = get_attractions(AttractionInput(destination="Goa", interests=["history"], limit=3))

    assert isinstance(result, list)
    assert len(result) <= 3
    assert all(isinstance(a, Attraction) for a in result)
    assert result[0].lat is not None  # lat/lng present (Phase 18 map)
    assert result[0].name == "Fort Aguada"
    assert result[0].category == "history"


def test_get_attractions_no_matching_interests_returns_results():
    """Even with niche interests, tool returns what OpenTripMap gives back
    (unmapped interests fall back to 'interesting_places' kind)."""
    geo_response = MagicMock()
    geo_response.raise_for_status = MagicMock()
    geo_response.json.return_value = {"lat": 15.4909, "lon": 73.8278}

    radius_response = MagicMock()
    radius_response.raise_for_status = MagicMock()
    radius_response.json.return_value = [
        {
            "name": "Some Place",
            "kinds": "interesting_places",
            "rate": 3,
            "point": {"lat": 15.0, "lon": 73.0},
        }
    ]

    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools.httpx") as mock_httpx,
    ):
        mock_httpx.get.side_effect = [geo_response, radius_response]
        result = get_attractions(AttractionInput(destination="Goa", interests=["nonexistent_interest"], limit=5))

    assert isinstance(result, list)
    assert len(result) > 0


def test_get_attractions_geocode_not_found():
    """Destination that OpenTripMap can't geocode → NOT_FOUND ToolError."""
    geo_response = MagicMock()
    geo_response.raise_for_status = MagicMock()
    geo_response.json.return_value = {}  # no lat/lon → geocode failed

    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools.httpx") as mock_httpx,
    ):
        mock_httpx.get.return_value = geo_response
        result = get_attractions(AttractionInput(destination="Nowhereville", interests=[], limit=3))

    assert isinstance(result, ToolError)
    assert result.code == "NOT_FOUND"


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

    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
    ):
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
                "dt": int(datetime.combine(tomorrow, datetime.min.time()).timestamp()),
                "weather": [{"main": "Sunny"}],
                "main": {"temp_max": 32.0, "temp_min": 24.0},
            }
        ]
    }

    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools.httpx") as mock_httpx,
    ):
        mock_httpx.get.return_value = mock_owm_response
        result = get_weather(WeatherInput(destination="Goa", date_range=date_range))

    assert isinstance(result, list)
    assert all(isinstance(d, DayForecast) for d in result)


# ── estimate_budget ────────────────────────────────────────────────────────


def test_estimate_budget_sums_correctly():
    result = estimate_budget(BudgetInput(flights=8_000, hotels=3_000, days=5, daily_spend=2_000))
    assert isinstance(result, BudgetEstimate)
    assert result.total == 8_000 + (3_000 * 5) + (2_000 * 5)


def test_estimate_budget_high_budget_note():
    result = estimate_budget(BudgetInput(flights=50_000, hotels=10_000, days=7, daily_spend=5_000))
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


def test_provider_errors_never_leak_the_api_key():
    """httpx puts the full request URL (apikey=…) in str(HTTPStatusError); it must not reach the ToolError."""
    import httpx

    request = httpx.Request("GET", "https://api.opentripmap.com/0.1/en/places/geoname?apikey=SECRET-KEY")
    denied = httpx.HTTPStatusError("401", request=request, response=httpx.Response(401, request=request))

    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _fake_settings(otm="SECRET-KEY", owm="SECRET-KEY")),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.httpx.get", side_effect=denied),
    ):
        attractions = get_attractions(AttractionInput(destination="Goa", interests=["beach"], limit=3))
        weather = get_weather(WeatherInput(destination="Goa", date_range=f"{date.today()} to {date.today()}"))

    for result, code in ((attractions, "OTM_ERROR"), (weather, "OWM_ERROR")):
        assert isinstance(result, ToolError) and result.code == code
        assert "SECRET-KEY" not in result.error and "401" in result.error

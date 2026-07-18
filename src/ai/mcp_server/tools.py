"""
Phase 3 — all 5 MCP tools wired to real external APIs.

Rules:
- Missing API key  → ToolError(code="API_NOT_CONFIGURED")  — server never crashes
- Validation first → ToolError before any network call
- Every tool caches; TTLs match Phase 24 spec (implemented early):
    flights     5 min   search_flights
    hotels     15 min   search_hotels
    attractions 6 hr    get_attractions
    weather     1 hr    get_weather
- estimate_budget is pure logic — no network, no cache needed
"""

import logging
import re
from datetime import date, datetime, timedelta

from amadeus import Client as AmadeusClient
from amadeus import ResponseError as AmadeusError
import googlemaps
import httpx

from src.ai.mcp_server.cache import get_cached_sync, make_cache_key, set_cached_sync
from src.ai.mcp_server.config import mcp_settings
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

logger = logging.getLogger(__name__)

TTL_FLIGHTS = 300        # 5 min
TTL_HOTELS = 900         # 15 min
TTL_ATTRACTIONS = 21_600  # 6 hr
TTL_WEATHER = 3_600      # 1 hr


# ── Helpers ────────────────────────────────────────────────────────────────

# Mapping of common Indian city names → Amadeus IATA city codes
_CITY_IATA: dict[str, str] = {
    "goa": "GOI", "mumbai": "BOM", "delhi": "DEL", "new delhi": "DEL",
    "bangalore": "BLR", "bengaluru": "BLR", "chennai": "MAA",
    "kolkata": "CCU", "hyderabad": "HYD", "jaipur": "JAI",
    "kochi": "COK", "cochin": "COK", "pune": "PNQ", "ahmedabad": "AMD",
    "agra": "AGR", "varanasi": "VNS", "amritsar": "ATQ", "guwahati": "GAU",
    "leh": "IXL", "srinagar": "SXR", "udaipur": "UDR", "jodhpur": "JDH",
    "aurangabad": "IXU", "nagpur": "NAG", "bhopal": "BHO", "indore": "IDR",
    "lucknow": "LKO", "patna": "PAT", "ranchi": "IXR", "bhubaneswar": "BBI",
    "visakhapatnam": "VTZ", "coimbatore": "CJB", "madurai": "IXM",
    "tiruchirappalli": "TRZ", "port blair": "IXZ", "shimla": "SLV",
    "dharamshala": "DHM", "dehradun": "DED", "raipur": "RPR",
    "vadodara": "BDQ", "surat": "STV",
}

# Google Place type → our category system
_PLACE_TYPE_MAP: dict[str, str] = {
    "museum": "history", "tourist_attraction": "sightseeing",
    "church": "history", "hindu_temple": "history", "mosque": "history",
    "restaurant": "food", "food": "food", "cafe": "food",
    "bar": "nightlife", "night_club": "nightlife",
    "park": "nature", "natural_feature": "nature", "campground": "nature",
    "amusement_park": "adventure", "zoo": "nature", "aquarium": "nature",
    "spa": "wellness", "shopping_mall": "shopping",
    "beach": "beach", "stadium": "sports",
}

# Monthly climate fallback for dates beyond OWM's 5-day window.
# Tuple: (temp_high_c, temp_low_c, condition)
_CLIMATE: dict[str, dict[int, tuple[float, float, str]]] = {
    "goa": {
        1: (31, 22, "Sunny"), 2: (32, 23, "Sunny"), 3: (34, 25, "Sunny"),
        4: (35, 27, "Partly Cloudy"), 5: (35, 28, "Partly Cloudy"),
        6: (32, 27, "Rainy"), 7: (30, 26, "Rainy"), 8: (30, 26, "Rainy"),
        9: (31, 26, "Partly Cloudy"), 10: (32, 26, "Partly Cloudy"),
        11: (32, 25, "Sunny"), 12: (31, 24, "Sunny"),
    },
    "mumbai": {
        1: (30, 20, "Sunny"), 2: (31, 21, "Sunny"), 3: (33, 23, "Sunny"),
        4: (35, 26, "Partly Cloudy"), 5: (36, 28, "Partly Cloudy"),
        6: (32, 27, "Rainy"), 7: (30, 26, "Rainy"), 8: (30, 26, "Rainy"),
        9: (32, 26, "Rainy"), 10: (33, 25, "Partly Cloudy"),
        11: (33, 23, "Sunny"), 12: (31, 21, "Sunny"),
    },
    "delhi": {
        1: (20, 7, "Sunny"), 2: (23, 10, "Sunny"), 3: (28, 15, "Sunny"),
        4: (36, 22, "Sunny"), 5: (40, 27, "Sunny"),
        6: (39, 29, "Partly Cloudy"), 7: (35, 28, "Rainy"), 8: (33, 27, "Rainy"),
        9: (33, 25, "Partly Cloudy"), 10: (32, 19, "Sunny"),
        11: (26, 12, "Sunny"), 12: (21, 7, "Sunny"),
    },
    "jaipur": {
        1: (21, 9, "Sunny"), 2: (24, 11, "Sunny"), 3: (30, 16, "Sunny"),
        4: (36, 22, "Sunny"), 5: (40, 27, "Sunny"),
        6: (39, 29, "Partly Cloudy"), 7: (35, 27, "Rainy"), 8: (33, 26, "Rainy"),
        9: (34, 24, "Partly Cloudy"), 10: (33, 19, "Sunny"),
        11: (27, 13, "Sunny"), 12: (22, 8, "Sunny"),
    },
    "kerala": {
        1: (32, 23, "Sunny"), 2: (33, 24, "Sunny"), 3: (34, 26, "Sunny"),
        4: (34, 27, "Partly Cloudy"), 5: (33, 27, "Rainy"),
        6: (30, 25, "Rainy"), 7: (29, 24, "Rainy"), 8: (29, 24, "Rainy"),
        9: (30, 25, "Rainy"), 10: (31, 25, "Partly Cloudy"),
        11: (32, 24, "Partly Cloudy"), 12: (31, 23, "Sunny"),
    },
}
_CLIMATE_DEFAULT: dict[int, tuple[float, float, str]] = {
    m: (32, 25, "Partly Cloudy") for m in range(1, 13)
}


def _city_to_iata(city: str) -> str | None:
    return _CITY_IATA.get(city.strip().lower())


def _parse_iso_duration(duration: str) -> int:
    """Parse 'PT2H15M' → total minutes."""
    hours = re.search(r"(\d+)H", duration)
    mins = re.search(r"(\d+)M", duration)
    return (int(hours.group(1)) if hours else 0) * 60 + (int(mins.group(1)) if mins else 0)


def _infer_category(types: list[str], interests: list[str]) -> str:
    for t in types:
        cat = _PLACE_TYPE_MAP.get(t)
        if cat:
            return cat
    return interests[0] if interests else "sightseeing"


def _climate_forecast(destination: str, start: date, num_days: int) -> list[DayForecast]:
    climate = _CLIMATE.get(destination.strip().lower(), _CLIMATE_DEFAULT)
    out = []
    for i in range(num_days):
        day = start + timedelta(days=i)
        high, low, cond = climate.get(day.month, (32, 25, "Partly Cloudy"))
        out.append(DayForecast(
            date=day.isoformat(),
            condition=f"{cond} (climate estimate)",
            temp_high_c=high,
            temp_low_c=low,
        ))
    return out


# ── Tool: search_flights ───────────────────────────────────────────────────


def search_flights(input: FlightSearchInput) -> list[Flight] | ToolError:
    """Search flights via Amadeus sandbox. Caches results for 5 min."""
    # --- validation (before any API call) ---
    if date.fromisoformat(input.date) < date.today():
        return ToolError(error="Departure date is in the past.", code="PAST_DATE")
    if input.budget < 2_000:
        return ToolError(
            error="Budget too low — minimum viable flight budget is ₹2,000.",
            code="BUDGET_TOO_LOW",
        )

    # --- API key check ---
    if not mcp_settings.AMADEUS_CLIENT_ID or not mcp_settings.AMADEUS_CLIENT_SECRET:
        return ToolError(
            error="Amadeus API not configured. Set AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET in .env.",
            code="API_NOT_CONFIGURED",
        )

    # --- cache ---
    cache_key = make_cache_key("flights", input.model_dump())
    cached = get_cached_sync(cache_key)
    if cached is not None:
        return [Flight(**f) for f in cached]

    # --- real API call ---
    try:
        amadeus = AmadeusClient(
            client_id=mcp_settings.AMADEUS_CLIENT_ID,
            client_secret=mcp_settings.AMADEUS_CLIENT_SECRET,
        )
        response = amadeus.shopping.flight_offers_search.get(
            originLocationCode=input.origin,
            destinationLocationCode=input.destination,
            departureDate=input.date,
            adults=input.passengers,
            currencyCode="INR",
            max=5,
        )
        flights: list[Flight] = []
        for offer in response.data:
            itin = offer["itineraries"][0]
            seg = itin["segments"][0]
            price = float(offer["price"]["grandTotal"]) * input.passengers
            flights.append(Flight(
                airline=seg["carrierCode"],
                flight_number=f"{seg['carrierCode']}-{seg['number']}",
                departure=seg["departure"]["at"],
                arrival=seg["arrival"]["at"],
                duration_mins=_parse_iso_duration(itin["duration"]),
                price_inr=price,
                stops=len(itin["segments"]) - 1,
            ))
        set_cached_sync(cache_key, [f.model_dump() for f in flights], TTL_FLIGHTS)
        return flights

    except AmadeusError as exc:
        logger.error("Amadeus flight search error: %s", exc)
        return ToolError(error=f"Amadeus error: {exc.description}", code="AMADEUS_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in search_flights")
        return ToolError(error=f"Unexpected error: {exc}", code="UNKNOWN_ERROR")


# ── Tool: search_hotels ────────────────────────────────────────────────────


def search_hotels(input: HotelSearchInput) -> list[Hotel] | ToolError:
    """Search hotels via Amadeus Hotel Search. Caches results for 15 min."""
    # --- validation ---
    if date.fromisoformat(input.check_out) <= date.fromisoformat(input.check_in):
        return ToolError(error="check_out must be after check_in.", code="INVALID_DATES")

    # --- API key check ---
    if not mcp_settings.AMADEUS_CLIENT_ID or not mcp_settings.AMADEUS_CLIENT_SECRET:
        return ToolError(
            error="Amadeus API not configured. Set AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET in .env.",
            code="API_NOT_CONFIGURED",
        )

    city_code = _city_to_iata(input.destination)
    if not city_code:
        return ToolError(
            error=(
                f"Unsupported destination: '{input.destination}'. "
                "Add it to _CITY_IATA in tools.py or pass the IATA city code directly."
            ),
            code="UNKNOWN_DESTINATION",
        )

    # --- cache ---
    cache_key = make_cache_key("hotels", input.model_dump())
    cached = get_cached_sync(cache_key)
    if cached is not None:
        return [Hotel(**h) for h in cached]

    # --- real API call (two-step: list hotels, then get offers) ---
    try:
        amadeus = AmadeusClient(
            client_id=mcp_settings.AMADEUS_CLIENT_ID,
            client_secret=mcp_settings.AMADEUS_CLIENT_SECRET,
        )

        # Step 1 — get hotel IDs for the city
        hotels_resp = amadeus.reference_data.locations.hotels.by_city.get(
            cityCode=city_code,
            radius=20,
            radiusUnit="KM",
        )
        hotel_ids = [h["hotelId"] for h in (hotels_resp.data or [])[:20]]
        if not hotel_ids:
            return ToolError(
                error=f"No hotels found in {input.destination}.", code="NO_RESULTS"
            )

        # Step 2 — get offers for those hotels
        offers_resp = amadeus.shopping.hotel_offers_search.get(
            hotelIds=",".join(hotel_ids),
            checkInDate=input.check_in,
            checkOutDate=input.check_out,
            adults=input.guests,
            currency="INR",
            bestRateOnly=True,
        )

        hotels: list[Hotel] = []
        for item in offers_resp.data or []:
            h_data = item.get("hotel", {})
            offers = item.get("offers", [])
            if not offers:
                continue
            price_dict = offers[0].get("price", {})
            price = float(price_dict.get("base") or price_dict.get("total") or 0)
            if price > input.budget_per_night:
                continue
            addr_parts = h_data.get("address", {})
            address = ", ".join(
                filter(None, [
                    (addr_parts.get("lines") or [""])[0],
                    addr_parts.get("cityName", input.destination),
                ])
            )
            hotels.append(Hotel(
                name=h_data.get("name", "Unknown Hotel"),
                stars=int(h_data.get("rating") or 3),
                price_per_night_inr=price,
                rating=float(h_data.get("rating") or 3.0),
                address=address or input.destination,
            ))

        if not hotels:
            return ToolError(
                error=f"No hotels within ₹{input.budget_per_night}/night in {input.destination}.",
                code="NO_RESULTS",
            )

        set_cached_sync(cache_key, [h.model_dump() for h in hotels], TTL_HOTELS)
        return hotels

    except AmadeusError as exc:
        logger.error("Amadeus hotel search error: %s", exc)
        return ToolError(error=f"Amadeus error: {exc.description}", code="AMADEUS_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in search_hotels")
        return ToolError(error=f"Unexpected error: {exc}", code="UNKNOWN_ERROR")


# ── Tool: get_attractions ──────────────────────────────────────────────────


def get_attractions(input: AttractionInput) -> list[Attraction] | ToolError:
    """Search attractions via Google Maps Places API. Caches for 6 hours."""
    # --- validation ---
    if input.limit > 10:
        return ToolError(
            error="Limit cannot exceed 10.", code="LIMIT_EXCEEDED"
        )

    # --- API key check ---
    if not mcp_settings.GOOGLE_MAPS_API_KEY:
        return ToolError(
            error="Google Maps API not configured. Set GOOGLE_MAPS_API_KEY in .env.",
            code="API_NOT_CONFIGURED",
        )

    # --- cache ---
    cache_key = make_cache_key("attractions", input.model_dump())
    cached = get_cached_sync(cache_key)
    if cached is not None:
        return [Attraction(**a) for a in cached]

    # --- real API call ---
    try:
        gmaps = googlemaps.Client(key=mcp_settings.GOOGLE_MAPS_API_KEY)
        interest_str = ", ".join(input.interests) if input.interests else "tourist"
        query = f"top {interest_str} attractions in {input.destination} India"
        result = gmaps.places(query=query, language="en")

        attractions: list[Attraction] = []
        for place in (result.get("results") or [])[:input.limit]:
            types = place.get("types", [])
            category = _infer_category(types, input.interests)
            loc = place.get("geometry", {}).get("location", {})
            editorial = place.get("editorial_summary", {}).get("overview")
            attractions.append(Attraction(
                name=place.get("name", ""),
                category=category,
                rating=float(place.get("rating") or 3.0),
                description=editorial or f"A popular {category} attraction in {input.destination}.",
                lat=loc.get("lat"),
                lng=loc.get("lng"),
            ))

        if not attractions:
            # Fall back gracefully — no error, empty list surfaced via NO_RESULTS
            return ToolError(
                error=f"No attractions found for {input.interests} in {input.destination}.",
                code="NO_RESULTS",
            )

        set_cached_sync(cache_key, [a.model_dump() for a in attractions], TTL_ATTRACTIONS)
        return attractions

    except Exception as exc:
        logger.exception("Unexpected error in get_attractions")
        return ToolError(error=f"Google Maps error: {exc}", code="MAPS_ERROR")


# ── Tool: get_weather ──────────────────────────────────────────────────────


def get_weather(input: WeatherInput) -> list[DayForecast] | ToolError:
    """
    Return weather forecast.

    - Within OWM's 5-day window  → real OWM API data
    - Beyond 5 days (trip planning future dates) → climate estimate
      labelled "(climate estimate)" so callers know the source
    Caches for 1 hour.
    """
    # --- validation ---
    if not input.destination.strip():
        return ToolError(error="Destination cannot be empty.", code="MISSING_DESTINATION")

    # --- API key check ---
    if not mcp_settings.OPENWEATHER_API_KEY:
        return ToolError(
            error="OpenWeatherMap API not configured. Set OPENWEATHER_API_KEY in .env.",
            code="API_NOT_CONFIGURED",
        )

    # --- parse date range ---
    try:
        parts = input.date_range.split(" to ")
        start_date = date.fromisoformat(parts[0].strip())
        end_date = (
            date.fromisoformat(parts[1].strip())
            if len(parts) > 1
            else start_date + timedelta(days=6)
        )
    except (ValueError, IndexError):
        return ToolError(
            error="Invalid date_range format. Use 'YYYY-MM-DD to YYYY-MM-DD'.",
            code="INVALID_DATE_RANGE",
        )

    num_days = max(1, min(7, (end_date - start_date).days + 1))
    days_until_start = (start_date - date.today()).days

    # --- cache ---
    cache_key = make_cache_key("weather", input.model_dump())
    cached = get_cached_sync(cache_key)
    if cached is not None:
        return [DayForecast(**d) for d in cached]

    # --- beyond OWM window → climate estimate ---
    if days_until_start > 4:
        forecasts = _climate_forecast(input.destination, start_date, num_days)
        set_cached_sync(cache_key, [d.model_dump() for d in forecasts], TTL_WEATHER)
        return forecasts

    # --- real OWM API call ---
    try:
        resp = httpx.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={
                "q": f"{input.destination},IN",
                "appid": mcp_settings.OPENWEATHER_API_KEY,
                "units": "metric",
                "cnt": 40,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        # Group 3-hour readings by day — accumulate max/min temperature
        days: dict[str, dict] = {}
        for item in data.get("list", []):
            dt = datetime.fromtimestamp(item["dt"])
            dk = dt.date().isoformat()
            if dk not in days:
                days[dk] = {
                    "condition": item["weather"][0]["main"],
                    "t_max": item["main"]["temp_max"],
                    "t_min": item["main"]["temp_min"],
                }
            else:
                days[dk]["t_max"] = max(days[dk]["t_max"], item["main"]["temp_max"])
                days[dk]["t_min"] = min(days[dk]["t_min"], item["main"]["temp_min"])

        forecasts = [
            DayForecast(
                date=dk,
                condition=info["condition"],
                temp_high_c=round(info["t_max"], 1),
                temp_low_c=round(info["t_min"], 1),
            )
            for dk, info in list(days.items())[:num_days]
        ]
        set_cached_sync(cache_key, [d.model_dump() for d in forecasts], TTL_WEATHER)
        return forecasts

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return ToolError(
                error=f"City not found in OpenWeatherMap: {input.destination}",
                code="NOT_FOUND",
            )
        return ToolError(error=f"OWM API error: {exc}", code="OWM_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in get_weather")
        return ToolError(error=f"Unexpected error: {exc}", code="UNKNOWN_ERROR")


# ── Tool: estimate_budget ──────────────────────────────────────────────────


def estimate_budget(input: BudgetInput) -> BudgetEstimate | ToolError:
    """
    Pure arithmetic — no external API.
    Phase 3 adds explicit validation so agents receive a ToolError
    instead of a Pydantic ValidationError on negative values.
    """
    if input.flights < 0:
        return ToolError(error="flights must be ≥ 0.", code="INVALID_INPUT")
    if input.hotels < 0:
        return ToolError(error="hotels must be ≥ 0.", code="INVALID_INPUT")
    if input.daily_spend < 0:
        return ToolError(error="daily_spend must be ≥ 0.", code="INVALID_INPUT")

    hotel_total = input.hotels * input.days
    activities = input.daily_spend * input.days
    total = input.flights + hotel_total + activities

    return BudgetEstimate(
        flights=input.flights,
        hotels=hotel_total,
        activities_estimate=activities,
        total=total,
        per_person=total,  # Phase 25 makes this per-person aware
        notes=(
            "Budget is within typical range."
            if total < 80_000
            else f"Budget is above ₹80,000 — consider cheaper alternatives."
        ),
    )

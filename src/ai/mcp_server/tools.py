"""
Phase 3 — all 5 MCP tools wired to real external APIs.

Providers: Duffel (flights), Amadeus (hotels), OpenTripMap (attractions),
OpenWeatherMap (weather). Amadeus closed its self-service portal on
2026-07-17, so search_hotels returns API_NOT_CONFIGURED until a replacement
hotel provider is wired in — the orchestrator plans without a hotel then.

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

import httpx
from amadeus import Client as AmadeusClient
from amadeus import ResponseError as AmadeusError

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

TTL_FLIGHTS = 300  # 5 min
TTL_HOTELS = 900  # 15 min
TTL_ATTRACTIONS = 21_600  # 6 hr
TTL_WEATHER = 3_600  # 1 hr


# ── Helpers ────────────────────────────────────────────────────────────────

DUFFEL_API_URL = "https://api.duffel.com"
MAX_FLIGHT_RESULTS = 5

# Duffel quotes offers in the account's billing currency and has no currency
# parameter, so prices are converted with these approximate rates. Offers in
# any other currency are skipped rather than mispriced.
_FX_TO_INR: dict[str, float] = {"INR": 1.0, "USD": 84.0, "EUR": 91.0, "GBP": 107.0}

# Mapping of common Indian city names → IATA city codes
_CITY_IATA: dict[str, str] = {
    "goa": "GOI",
    "mumbai": "BOM",
    "delhi": "DEL",
    "new delhi": "DEL",
    "bangalore": "BLR",
    "bengaluru": "BLR",
    "chennai": "MAA",
    "kolkata": "CCU",
    "hyderabad": "HYD",
    "jaipur": "JAI",
    "kochi": "COK",
    "cochin": "COK",
    "pune": "PNQ",
    "ahmedabad": "AMD",
    "agra": "AGR",
    "varanasi": "VNS",
    "amritsar": "ATQ",
    "guwahati": "GAU",
    "leh": "IXL",
    "srinagar": "SXR",
    "udaipur": "UDR",
    "jodhpur": "JDH",
    "aurangabad": "IXU",
    "nagpur": "NAG",
    "bhopal": "BHO",
    "indore": "IDR",
    "lucknow": "LKO",
    "patna": "PAT",
    "ranchi": "IXR",
    "bhubaneswar": "BBI",
    "visakhapatnam": "VTZ",
    "coimbatore": "CJB",
    "madurai": "IXM",
    "tiruchirappalli": "TRZ",
    "port blair": "IXZ",
    "shimla": "SLV",
    "dharamshala": "DHM",
    "dehradun": "DED",
    "raipur": "RPR",
    "vadodara": "BDQ",
    "surat": "STV",
}

# OpenTripMap kind → our category system
_OTM_KIND_MAP: dict[str, str] = {
    "museums": "history",
    "historic": "history",
    "religion": "history",
    "architecture": "sightseeing",
    "cultural": "history",
    "foods": "food",
    "restaurants": "food",
    "cafes": "food",
    "nightclubs": "nightlife",
    "natural": "nature",
    "national_parks": "nature",
    "beaches": "beach",
    "amusements": "adventure",
    "sport": "sports",
    "adult": "nightlife",
    "shops": "shopping",
    "spas": "wellness",
}

_INTEREST_TO_OTM_KIND: dict[str, str] = {
    "history": "historic,museums,cultural",
    "food": "foods,restaurants,cafes",
    "beach": "beaches",
    "nature": "natural,national_parks",
    "adventure": "amusements,sport",
    "nightlife": "nightclubs",
    "shopping": "shops",
    "wellness": "spas",
    "sightseeing": "interesting_places",
    "culture": "cultural,museums",
}

# Monthly climate fallback for dates beyond OWM's 5-day window.
# Tuple: (temp_high_c, temp_low_c, condition)
_CLIMATE: dict[str, dict[int, tuple[float, float, str]]] = {
    "goa": {
        1: (31, 22, "Sunny"),
        2: (32, 23, "Sunny"),
        3: (34, 25, "Sunny"),
        4: (35, 27, "Partly Cloudy"),
        5: (35, 28, "Partly Cloudy"),
        6: (32, 27, "Rainy"),
        7: (30, 26, "Rainy"),
        8: (30, 26, "Rainy"),
        9: (31, 26, "Partly Cloudy"),
        10: (32, 26, "Partly Cloudy"),
        11: (32, 25, "Sunny"),
        12: (31, 24, "Sunny"),
    },
    "mumbai": {
        1: (30, 20, "Sunny"),
        2: (31, 21, "Sunny"),
        3: (33, 23, "Sunny"),
        4: (35, 26, "Partly Cloudy"),
        5: (36, 28, "Partly Cloudy"),
        6: (32, 27, "Rainy"),
        7: (30, 26, "Rainy"),
        8: (30, 26, "Rainy"),
        9: (32, 26, "Rainy"),
        10: (33, 25, "Partly Cloudy"),
        11: (33, 23, "Sunny"),
        12: (31, 21, "Sunny"),
    },
    "delhi": {
        1: (20, 7, "Sunny"),
        2: (23, 10, "Sunny"),
        3: (28, 15, "Sunny"),
        4: (36, 22, "Sunny"),
        5: (40, 27, "Sunny"),
        6: (39, 29, "Partly Cloudy"),
        7: (35, 28, "Rainy"),
        8: (33, 27, "Rainy"),
        9: (33, 25, "Partly Cloudy"),
        10: (32, 19, "Sunny"),
        11: (26, 12, "Sunny"),
        12: (21, 7, "Sunny"),
    },
    "jaipur": {
        1: (21, 9, "Sunny"),
        2: (24, 11, "Sunny"),
        3: (30, 16, "Sunny"),
        4: (36, 22, "Sunny"),
        5: (40, 27, "Sunny"),
        6: (39, 29, "Partly Cloudy"),
        7: (35, 27, "Rainy"),
        8: (33, 26, "Rainy"),
        9: (34, 24, "Partly Cloudy"),
        10: (33, 19, "Sunny"),
        11: (27, 13, "Sunny"),
        12: (22, 8, "Sunny"),
    },
    "kerala": {
        1: (32, 23, "Sunny"),
        2: (33, 24, "Sunny"),
        3: (34, 26, "Sunny"),
        4: (34, 27, "Partly Cloudy"),
        5: (33, 27, "Rainy"),
        6: (30, 25, "Rainy"),
        7: (29, 24, "Rainy"),
        8: (29, 24, "Rainy"),
        9: (30, 25, "Rainy"),
        10: (31, 25, "Partly Cloudy"),
        11: (32, 24, "Partly Cloudy"),
        12: (31, 23, "Sunny"),
    },
}
_CLIMATE_DEFAULT: dict[int, tuple[float, float, str]] = {m: (32, 25, "Partly Cloudy") for m in range(1, 13)}


def _city_to_iata(city: str) -> str | None:
    return _CITY_IATA.get(city.strip().lower())


def _to_airport_code(place: str) -> str | None:
    """Accept a known city name ("Goa") or an IATA code ("GOI"); None if neither.

    The city table is tried first because some city names are also 3-letter
    strings ("Goa" must resolve to GOI, not "GOA").
    """
    place = place.strip()
    return _city_to_iata(place) or (place.upper() if len(place) == 3 and place.isalpha() else None)


def _parse_iso_duration(duration: str) -> int:
    """Parse 'PT2H15M' / 'P1DT2H' → total minutes."""
    days, hours, mins = (re.search(rf"(\d+){unit}", duration) for unit in "DHM")
    return sum(int(m.group(1)) * factor for m, factor in ((days, 1440), (hours, 60), (mins, 1)) if m)


def _otm_kind_to_category(kinds: str) -> str:
    for k in kinds.split(","):
        cat = _OTM_KIND_MAP.get(k.strip())
        if cat:
            return cat
    return "sightseeing"


def _climate_forecast(destination: str, start: date, num_days: int) -> list[DayForecast]:
    climate = _CLIMATE.get(destination.strip().lower(), _CLIMATE_DEFAULT)
    out = []
    for i in range(num_days):
        day = start + timedelta(days=i)
        high, low, cond = climate.get(day.month, (32, 25, "Partly Cloudy"))
        out.append(
            DayForecast(
                date=day.isoformat(),
                condition=f"{cond} (climate estimate)",
                temp_high_c=high,
                temp_low_c=low,
            )
        )
    return out


# ── Tool: search_flights ───────────────────────────────────────────────────


def _offer_to_flight(offer: dict, budget: float) -> Flight | None:
    """Map one Duffel offer to a Flight, or None if it is unpriceable or over budget."""
    rate = _FX_TO_INR.get(offer.get("total_currency", ""))
    if rate is None:
        return None
    price = round(float(offer["total_amount"]) * rate, 2)  # total_amount already covers all passengers
    if price > budget:
        return None

    outbound = offer["slices"][0]
    first, last = outbound["segments"][0], outbound["segments"][-1]
    carrier = first["marketing_carrier"]["iata_code"]
    return Flight(
        airline=carrier,
        flight_number=f"{carrier}-{first['marketing_carrier_flight_number']}",
        departure=first["departing_at"],
        arrival=last["arriving_at"],
        duration_mins=_parse_iso_duration(outbound.get("duration") or ""),
        price_inr=price,
        stops=len(outbound["segments"]) - 1,
    )


def search_flights(input: FlightSearchInput) -> list[Flight] | ToolError:
    """Search flights via Duffel, cheapest first, capped at `budget`. Caches results for 5 min.

    With a return_date the search is a round trip and price_inr is the total
    for both directions; the other Flight fields describe the outbound leg.
    """
    # --- validation (before any API call) ---
    try:
        if date.fromisoformat(input.date) < date.today():
            return ToolError(error="Departure date is in the past.", code="PAST_DATE")
        if input.return_date and date.fromisoformat(input.return_date) < date.fromisoformat(input.date):
            return ToolError(error="return_date is before the departure date.", code="INVALID_DATES")
    except ValueError:
        return ToolError(error="Dates must be ISO 8601 (YYYY-MM-DD).", code="INVALID_DATES")
    if input.budget < 2_000:
        return ToolError(
            error="Budget too low — minimum viable flight budget is ₹2,000.",
            code="BUDGET_TOO_LOW",
        )

    # --- API key check ---
    if not mcp_settings.DUFFEL_ACCESS_TOKEN:
        return ToolError(
            error="Duffel API not configured. Set DUFFEL_ACCESS_TOKEN in .env.",
            code="API_NOT_CONFIGURED",
        )

    origin, destination = _to_airport_code(input.origin), _to_airport_code(input.destination)
    if not origin or not destination:
        return ToolError(
            error=(
                f"Unknown airport: '{input.destination if origin else input.origin}'. "
                "Pass an IATA code or add the city to _CITY_IATA in tools.py."
            ),
            code="UNKNOWN_DESTINATION",
        )

    # --- cache ---
    cache_key = make_cache_key("flights", input.model_dump())
    cached = get_cached_sync(cache_key)
    if cached is not None:
        return [Flight(**f) for f in cached]

    # --- real API call ---
    slices = [{"origin": origin, "destination": destination, "departure_date": input.date}]
    if input.return_date:
        slices.append({"origin": destination, "destination": origin, "departure_date": input.return_date})
    try:
        resp = httpx.post(
            f"{DUFFEL_API_URL}/air/offer_requests",
            params={"return_offers": "true", "supplier_timeout": 15_000},
            headers={
                "Authorization": f"Bearer {mcp_settings.DUFFEL_ACCESS_TOKEN}",
                "Duffel-Version": "v2",
                "Accept": "application/json",
            },
            json={
                "data": {
                    "slices": slices,
                    "passengers": [{"type": "adult"}] * input.passengers,
                    "cabin_class": "economy",
                    "max_connections": input.max_stops,
                }
            },
            timeout=30,
        )
        resp.raise_for_status()
        offers = resp.json()["data"].get("offers") or []

        flights = [f for f in (_offer_to_flight(offer, input.budget) for offer in offers) if f is not None]
        if not flights:
            return ToolError(
                error=f"No flights from {origin} to {destination} within ₹{input.budget:,.0f}.",
                code="NO_RESULTS",
            )
        flights = sorted(flights, key=lambda f: f.price_inr)[:MAX_FLIGHT_RESULTS]

        if input.preferred_airlines:
            preferred = {code.strip().upper() for code in input.preferred_airlines}
            # Soft preference: list.sort is stable, so preferred carriers move to the front
            # while price order is preserved within each group. Nothing is dropped.
            flights.sort(key=lambda f: f.airline.upper() not in preferred)
        set_cached_sync(cache_key, [f.model_dump() for f in flights], TTL_FLIGHTS)
        return flights

    except httpx.HTTPStatusError as exc:
        logger.error("Duffel flight search error: %s", exc)
        return ToolError(error=f"Duffel API error: HTTP {exc.response.status_code}", code="DUFFEL_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in search_flights")
        return ToolError(error=f"Unexpected error: {exc}", code="UNKNOWN_ERROR")


# ── Tool: search_hotels ────────────────────────────────────────────────────


def search_hotels(input: HotelSearchInput) -> list[Hotel] | ToolError:
    """Search hotels via Amadeus Hotel Search. Caches results for 15 min."""
    # --- validation ---
    try:
        if date.fromisoformat(input.check_out) <= date.fromisoformat(input.check_in):
            return ToolError(error="check_out must be after check_in.", code="INVALID_DATES")
    except ValueError:
        return ToolError(error="Dates must be ISO 8601 (YYYY-MM-DD).", code="INVALID_DATES")

    # --- API key check ---
    if not mcp_settings.AMADEUS_CLIENT_ID or not mcp_settings.AMADEUS_CLIENT_SECRET:
        return ToolError(
            error="Hotel search not configured (Amadeus self-service keys stopped working on 2026-07-17).",
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
            return ToolError(error=f"No hotels found in {input.destination}.", code="NO_RESULTS")

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
                filter(
                    None,
                    [
                        (addr_parts.get("lines") or [""])[0],
                        addr_parts.get("cityName", input.destination),
                    ],
                )
            )
            hotels.append(
                Hotel(
                    name=h_data.get("name", "Unknown Hotel"),
                    stars=int(h_data.get("rating") or 3),
                    price_per_night_inr=price,
                    rating=float(h_data.get("rating") or 3.0),
                    address=address or input.destination,
                )
            )

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
    """Search attractions via OpenTripMap. Caches for 6 hours."""
    if input.limit > 10:
        return ToolError(error="Limit cannot exceed 10.", code="LIMIT_EXCEEDED")

    if not mcp_settings.OPENTRIPMAP_API_KEY:
        return ToolError(
            error="OpenTripMap API not configured. Set OPENTRIPMAP_API_KEY in .env.",
            code="API_NOT_CONFIGURED",
        )

    cache_key = make_cache_key("attractions", input.model_dump())
    cached = get_cached_sync(cache_key)
    if cached is not None:
        return [Attraction(**a) for a in cached]

    try:
        # Step 1 — geocode destination name to lat/lon
        geo_resp = httpx.get(
            "https://api.opentripmap.com/0.1/en/places/geoname",
            params={"name": input.destination, "apikey": mcp_settings.OPENTRIPMAP_API_KEY},
            timeout=10,
        )
        geo_resp.raise_for_status()
        geo = geo_resp.json()
        if "lat" not in geo or "lon" not in geo:
            return ToolError(error=f"Could not geocode destination: {input.destination}", code="NOT_FOUND")

        # Step 2 — map interests to OTM kinds
        kinds = (
            ",".join({_INTEREST_TO_OTM_KIND.get(i.strip().lower(), "interesting_places") for i in input.interests})
            if input.interests
            else "interesting_places"
        )

        radius_resp = httpx.get(
            "https://api.opentripmap.com/0.1/en/places/radius",
            params={
                "radius": 15000,
                "lon": geo["lon"],
                "lat": geo["lat"],
                "kinds": kinds,
                "limit": input.limit,
                "rate": 2,
                "format": "json",
                "apikey": mcp_settings.OPENTRIPMAP_API_KEY,
            },
            timeout=10,
        )
        radius_resp.raise_for_status()
        places = radius_resp.json()

        if not places:
            return ToolError(
                error=f"No attractions found for {input.interests} in {input.destination}.",
                code="NO_RESULTS",
            )

        attractions: list[Attraction] = []
        for place in places[: input.limit]:
            category = _otm_kind_to_category(place.get("kinds", ""))
            attractions.append(
                Attraction(
                    name=place.get("name") or "Unnamed attraction",
                    category=category,
                    rating=float(place.get("rate") or 3.0),
                    description=f"A popular {category} attraction in {input.destination}.",
                    lat=place.get("point", {}).get("lat"),
                    lng=place.get("point", {}).get("lon"),
                )
            )

        set_cached_sync(cache_key, [a.model_dump() for a in attractions], TTL_ATTRACTIONS)
        return attractions

    except httpx.HTTPStatusError as exc:
        # str(exc) contains the request URL, API key included — report the status only.
        logger.error("OpenTripMap error: HTTP %s", exc.response.status_code)
        return ToolError(error=f"OpenTripMap API error: HTTP {exc.response.status_code}", code="OTM_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in get_attractions")
        return ToolError(error=f"Unexpected error: {exc}", code="UNKNOWN_ERROR")


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
        end_date = date.fromisoformat(parts[1].strip()) if len(parts) > 1 else start_date + timedelta(days=6)
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
        return ToolError(error=f"OWM API error: HTTP {exc.response.status_code}", code="OWM_ERROR")
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
            else "Budget is above ₹80,000 — consider cheaper alternatives."
        ),
    )

"""
Phase 3 — all 5 MCP tools wired to real external APIs.

Providers: Duffel (flights), LiteAPI (hotels), OpenTripMap (attractions),
OpenWeatherMap (weather), Nominatim / OpenStreetMap (geocoding, no key).

Rules:
- Missing API key  → ToolError(code="API_NOT_CONFIGURED")  — server never crashes
- Validation first → ToolError before any network call
- Every tool caches; the TTLs are the Phase 3 spec's, read back from Redis in Phase 24:
    flights     5 min   search_flights
    hotels     15 min   search_hotels
    attractions 6 hr    get_attractions
    weather     1 hr    get_weather
- estimate_budget is pure logic — no network, no cache needed

Phase 24 — how a request reaches a provider:
- The tools run in worker threads, several at once (server.py). Nothing here may
  assume it is the only search running.
- A flight or hotel search is cached by what the provider is asked. The budget
  and the preferred airlines are applied to the answer afterwards, so the same
  search under another budget is a cache hit — and a trip's caches can be warmed
  before its budget for hotels is known.
- One search per cache key at a time (cache.single_flight): a second identical
  search waits for the first and reads its answer from the cache.
- Every request goes out through outbound.send(): in the provider's rate limit's
  turn, and again after a backoff when it is answered 429 or 5xx. A provider that
  still says 429 after that is ToolError(code="RATE_LIMITED").

Phase 25 — what an attraction search finds, and for which interest:
- Every attraction says which of the interests asked for it was found under
  (`interests`). A group's searches are told apart by it.
- An interest with fewer well-known places than asked for is topped up with the
  lesser-known ones: for food, the provider rates almost nothing as well known.
- The results are cut to the limit one interest at a time, so no interest is cut
  out by the ones before it.
- "spa" and "wellness" are searched as nothing: the provider has no such places,
  and no category by that name (asking for one fails the whole request).
"""

import logging
import re
from datetime import date, datetime, timedelta
from itertools import zip_longest

import httpx

from src.ai import pricing
from src.ai.mcp_server.cache import get_cached_sync, make_cache_key, set_cached_sync, single_flight
from src.ai.mcp_server.config import mcp_settings
from src.ai.mcp_server.models import (
    Airport,
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
from src.ai.mcp_server.outbound import send
from src.ai.mcp_server.rate_limiter import RateLimited

logger = logging.getLogger(__name__)

# httpx logs every request URL at INFO, and OpenTripMap / OpenWeatherMap take
# their API key in the query string — keep those URLs out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

TTL_FLIGHTS = 300  # 5 min
TTL_HOTELS = 900  # 15 min
TTL_ATTRACTIONS = 21_600  # 6 hr
TTL_WEATHER = 3_600  # 1 hr
ATTRACTION_RADIUS_M = 30_000  # wide enough to reach the coast from a region's centre ("Goa")
# OpenTripMap's `rate` — the least popularity a place returned may have: 1 to 3, and the same scale
# again as 5 to 7 for a heritage site. 2 and up is what is searched first.
WELL_KNOWN, ANY_RATED = 2, 1
TTL_GEOCODE = 2_592_000  # 30 days — places don't move, and Nominatim asks clients to cache

COUNTRY_CODE = "IN"  # the planner covers trips within India (INR budgets, Indian airports)
COUNTRY_NAME = "India"


# ── Helpers ────────────────────────────────────────────────────────────────

DUFFEL_API_URL = "https://api.duffel.com"
LITEAPI_URL = "https://api.liteapi.travel/v3.0"
MAX_FLIGHT_RESULTS = 5
MAX_HOTEL_RESULTS = 5
HOTEL_SEARCH_RADIUS_M = 25_000

# Duffel quotes offers in the account's billing currency and has no currency
# parameter, so prices are converted with these approximate rates. Offers in
# any other currency are skipped rather than mispriced. (LiteAPI is asked for
# INR directly; the table only matters if it answers in something else.)
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
    "ayodhya": "AYJ",
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

# OpenTripMap kind → our category. A place carries several kinds — a wildlife
# sanctuary comes back as "cultural,museums,gardens_and_parks,natural,nature_reserves,zoos"
# — and the first entry here that it has wins, so the specific kinds come
# before the broad ones. Anything unmatched is "sightseeing".
_OTM_KIND_MAP: dict[str, str] = {
    "beaches": "beach",
    "nature_reserves": "nature",
    "national_parks": "nature",
    "zoos": "nature",
    "waterfalls": "nature",
    "caves": "nature",
    "mountain_peaks": "nature",
    "fortifications": "history",
    "castles": "history",
    "palaces": "history",
    "archaeology": "history",
    "monuments_and_memorials": "history",
    "historic": "history",
    "historic_architecture": "history",
    "religion": "spiritual",
    "museums": "museum",
    "foods": "food",
    "restaurants": "food",
    "cafes": "food",
    "nightclubs": "nightlife",
    "adult": "nightlife",
    "cinemas": "culture",
    "theatres_and_entertainments": "culture",
    "shops": "shopping",
    "amusements": "adventure",
    "climbing": "adventure",
    "winter_sports": "adventure",
    "sport": "sports",
    "gardens_and_parks": "nature",
    "natural": "nature",
    "architecture": "sightseeing",
    "cultural": "culture",
}

# The outdoors an adventure is made of, as far as the provider knows it: peaks and caves, waterfalls,
# reserves to trek or go on safari in, climbing, the water sports, the slopes, and water and amusement parks.
_OUTDOORS = "geological_formations,waterfalls,nature_reserves"

# Interest → the OpenTripMap kinds it is searched as. Every kind here was checked against the live
# catalogue (2026-10-05): a kind the provider does not know fails the whole request with HTTP 400,
# which is what "wellness": "spas" did until Phase 25. An interest mapped to "" is one the provider
# has no places for — it is searched as nothing, and said to have found nothing.
_INTEREST_TO_OTM_KIND: dict[str, str] = {
    "history": "historic,museums,cultural",
    "heritage": "historic,museums,cultural",
    "food": "foods,restaurants,cafes",
    "beach": "beaches",
    "nature": "natural,national_parks",
    # was "amusements,sport" — near Goa and Jaipur that is cricket stadiums
    "adventure": f"{_OUTDOORS},climbing,diving,surfing,kitesurfing,winter_sports,amusements",
    "trekking": _OUTDOORS,
    "hiking": _OUTDOORS,
    "nightlife": "nightclubs",
    "shopping": "shops",
    "sightseeing": "interesting_places",
    "culture": "cultural,museums",
    "museum": "museums",
    "temple": "religion",
    "religion": "religion",
    "spiritual": "religion",
    "architecture": "architecture",
    "wildlife": "natural,national_parks",
    # was "beaches,natural" — "natural" is every cave, peak and reserve: an adventure, not a rest
    "relaxation": "beaches,gardens_and_parks,water,view_points",
    # No spas, baths, saunas or springs near any destination tried, and no kind called "spas".
    "wellness": "",
    "spa": "",
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


class OutsideCoverage(Exception):
    """The destination is a real place, but not in the country the planner covers."""

    def __init__(self, destination: str, country: str):
        self.country = country
        super().__init__(
            f"{destination.strip()} is in {country}. This planner covers trips within {COUNTRY_NAME} for now."
        )


def _nominatim(query: str, country_code: str | None = None) -> list[dict]:
    """Up to five Nominatim matches, best first. Raises httpx errors."""
    params = {"q": query, "format": "json", "limit": 5, "addressdetails": 1, "accept-language": "en"}
    if country_code:
        params["countrycodes"] = country_code.lower()
    resp = send(
        "nominatim",
        lambda: httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params=params,
            headers={"User-Agent": "tripplanner-ai (github.com/shauryaman28/tripplanner-ai)"},
            timeout=10,
        ),
    )
    return resp.json()


def _best_place(matches: list[dict]) -> dict | None:
    """The first match that is somewhere you can travel to: a settlement or region, else a natural feature.

    Never a road, shop or building that merely shares the name — "London"
    searched inside India is "The London Bridge", a road in Pune.
    """
    for classes in (("place", "boundary"), ("natural",)):
        if found := next((m for m in matches if m.get("class") in classes), None):
            return found
    return None


# Below this Nominatim importance an in-country match is a minor place; a famous namesake abroad
# is then the likelier meaning ("Bali" is also a town in Rajasthan, "Dubai" a village in Kerala).
_CONFIDENT_IMPORTANCE = 0.3


def _geocode(destination: str) -> tuple[float, float] | None:
    """(lat, lon) of a destination, or None if no such place is known.

    Raises OutsideCoverage when the name means a place in another country, and
    httpx errors when Nominatim cannot be reached.

    Nominatim ranks by prominence, so "Manali" is the hill station, not the
    Chennai suburb a population-ranked geocoder returns. The in-country search
    comes first; a weak or missing match there is checked against the whole
    world, so a foreign city is reported as such instead of being planned as
    whatever shares its name at home.
    """
    cache_key = make_cache_key("geocode:v2", {"q": destination.strip().lower()})
    # One lookup per place at a time — the hotel and the attractions search of a plan both ask for the
    # same one, and the second finds it cached. Nominatim's 1 request/s is kept by its rate limit (send).
    with single_flight(cache_key):
        cached = get_cached_sync(cache_key)
        if cached is None:
            cached = _lookup_place(destination)
            set_cached_sync(cache_key, cached, TTL_GEOCODE)

    if cached.get("outside"):
        raise OutsideCoverage(destination, cached["outside"])
    return (cached["lat"], cached["lon"]) if "lat" in cached else None


def _lookup_place(destination: str) -> dict:
    """{"lat", "lon"} for a place in the covered country, {"outside": country} for one abroad, {} if unknown."""

    def located(match: dict) -> dict:
        return {"lat": float(match["lat"]), "lon": float(match["lon"])}

    def importance(match: dict | None) -> float:
        return float((match or {}).get("importance") or 0)

    at_home = _nominatim(destination, COUNTRY_CODE)
    local = _best_place(at_home)
    if importance(local) >= _CONFIDENT_IMPORTANCE:
        return located(local)

    best = _best_place(_nominatim(destination))
    address = (best or {}).get("address") or {}
    abroad = best is not None and address.get("country_code", "").upper() != COUNTRY_CODE
    # A namesake abroad wins only when it is clearly the better-known place.
    if abroad and importance(best) >= importance(local) + 0.2:
        return {"outside": address.get("country") or "another country"}
    # Otherwise the in-country match stands — even one that is only a station or a landmark
    # ("Alleppey"), when nothing else answers to the name.
    if match := local or (None if abroad else best) or (at_home[0] if at_home else None):
        return located(match)
    return {}


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


def _interest_to_otm_kinds(interest: str) -> str:
    """OpenTripMap kinds for one interest; plurals match too ("beaches" → beach).

    "" for an interest the provider has no places for; the general sights for one nobody listed.
    """
    key = interest.strip().lower()
    for candidate in (key, key.removesuffix("es"), key.removesuffix("s")):
        if candidate in _INTEREST_TO_OTM_KIND:
            return _INTEREST_TO_OTM_KIND[candidate]
    return "interesting_places"


def _otm_kind_to_category(kinds: str) -> str:
    place_kinds = {k.strip() for k in kinds.split(",")}
    return next((category for kind, category in _OTM_KIND_MAP.items() if kind in place_kinds), "sightseeing")


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


def _rate_limited(exc: RateLimited) -> ToolError:
    """A provider that is being asked too often. The traveller is told to try again (src/ai/utils/failures.py)."""
    logger.warning("Rate limited — %s", exc)
    return ToolError(error=f"Rate limited — {exc}.", code="RATE_LIMITED")


def _offer_to_flight(offer: dict) -> Flight | None:
    """Map one Duffel offer to a Flight, or None if it cannot be priced in rupees."""
    rate = _FX_TO_INR.get(offer.get("total_currency", ""))
    if rate is None:
        return None
    price = round(float(offer["total_amount"]) * rate, 2)  # total_amount already covers all passengers

    outbound = offer["slices"][0]
    first, last = outbound["segments"][0], outbound["segments"][-1]
    carrier = first["marketing_carrier"]["iata_code"]

    def airport(place: dict | None) -> Airport | None:
        if not place or not place.get("iata_code"):
            return None
        return Airport(
            code=place["iata_code"], name=place.get("name"), lat=place.get("latitude"), lng=place.get("longitude")
        )

    return Flight(
        airline=carrier,
        flight_number=f"{carrier}-{first['marketing_carrier_flight_number']}",
        departure=first["departing_at"],
        arrival=last["arriving_at"],
        duration_mins=_parse_iso_duration(outbound.get("duration") or ""),
        price_inr=price,
        stops=len(outbound["segments"]) - 1,
        origin=airport(first.get("origin")),
        destination=airport(last.get("destination")),
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

    # --- cache, then Duffel ---
    try:
        on_offer = _flights_on_offer(origin, destination, input)
    except RateLimited as exc:
        return _rate_limited(exc)
    except httpx.HTTPStatusError as exc:
        logger.error("Duffel flight search error: %s", exc)
        return ToolError(error=f"Duffel API error: HTTP {exc.response.status_code}", code="DUFFEL_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in search_flights")
        return ToolError(error=f"Unexpected error: {exc}", code="UNKNOWN_ERROR")

    # --- this traveller's budget and airlines, applied to the shared answer ---
    flights = [f for f in on_offer if f.price_inr <= input.budget]
    if not flights:
        return ToolError(
            error=f"No flights from {origin} to {destination} within ₹{input.budget:,.0f}.",
            code="NO_RESULTS",
        )
    if input.preferred_airlines:
        preferred = {code.strip().upper() for code in input.preferred_airlines}
        # Soft preference: list.sort is stable, so preferred carriers move to the front
        # while price order is preserved within each group. Nothing is dropped.
        flights.sort(key=lambda f: f.airline.upper() not in preferred)
    return flights


def _flights_on_offer(origin: str, destination: str, input: FlightSearchInput) -> list[Flight]:
    """The cheapest flights Duffel has for this search, whatever the budget. Raises httpx errors and RateLimited.

    Cached for 5 minutes under what Duffel is asked — the airports, the dates,
    the passengers and the connections allowed. The budget is not part of it:
    of the offers within any budget, the cheapest five are always among the
    cheapest five of all, so those are all that need keeping.
    """
    asked = {
        "origin": origin,
        "destination": destination,
        "date": input.date,
        "return_date": input.return_date,
        "passengers": input.passengers,
        "max_stops": input.max_stops,
    }
    cache_key = make_cache_key("flights", asked)
    with single_flight(cache_key):
        cached = get_cached_sync(cache_key)
        if cached is not None:
            return [Flight(**f) for f in cached]

        slices = [{"origin": origin, "destination": destination, "departure_date": input.date}]
        if input.return_date:
            slices.append({"origin": destination, "destination": origin, "departure_date": input.return_date})
        resp = send(
            "duffel",
            lambda: httpx.post(
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
            ),
        )
        offers = resp.json()["data"].get("offers") or []
        flights = sorted((f for f in map(_offer_to_flight, offers) if f is not None), key=lambda f: f.price_inr)
        flights = flights[:MAX_FLIGHT_RESULTS]
        if flights:  # an empty answer is not kept: the next search asks again
            set_cached_sync(cache_key, [f.model_dump() for f in flights], TTL_FLIGHTS)
        return flights


# ── Tool: search_hotels ────────────────────────────────────────────────────


def _to_hotel(info: dict, room_types: list[dict], nights: int, destination: str) -> Hotel | None:
    """Map one LiteAPI hotel + its offers to a Hotel priced at its cheapest offer per night."""
    offers = [rt["offerRetailRate"] for rt in room_types if rt.get("offerRetailRate")]
    priced = [(o["amount"] * _FX_TO_INR[o["currency"]], o) for o in offers if o.get("currency") in _FX_TO_INR]
    if not priced:
        return None
    total_inr = min(amount for amount, _ in priced)  # LiteAPI prices the whole stay, all rooms
    return Hotel(
        name=info.get("name") or "Unknown Hotel",
        stars=int(info.get("stars") or 0),
        price_per_night_inr=round(total_inr / nights, 2),
        rating=float(info.get("rating") or 0.0),
        address=", ".join(filter(None, [info.get("address"), info.get("city_name")])) or destination,
        lat=info.get("latitude"),
        lng=info.get("longitude"),
    )


def search_hotels(input: HotelSearchInput) -> list[Hotel] | ToolError:
    """Search hotels via LiteAPI, cheapest first, capped at `budget_per_night`. Caches results for 15 min.

    `budget_per_night` and `price_per_night_inr` are for the whole party: guests
    are put two to a room and the nightly price covers every room.
    """
    # --- validation ---
    try:
        nights = (date.fromisoformat(input.check_out) - date.fromisoformat(input.check_in)).days
    except ValueError:
        return ToolError(error="Dates must be ISO 8601 (YYYY-MM-DD).", code="INVALID_DATES")
    if nights <= 0:
        return ToolError(error="check_out must be after check_in.", code="INVALID_DATES")

    # --- API key check ---
    if not mcp_settings.LITEAPI_API_KEY:
        return ToolError(
            error="LiteAPI not configured. Set LITEAPI_API_KEY in .env.",
            code="API_NOT_CONFIGURED",
        )

    # --- cache, then LiteAPI ---
    try:
        on_offer = _hotels_on_offer(input, nights)
    except OutsideCoverage as exc:
        return ToolError(error=str(exc), code="OUTSIDE_COVERAGE")
    except RateLimited as exc:
        return _rate_limited(exc)
    except httpx.HTTPStatusError as exc:
        logger.error("LiteAPI hotel search error: HTTP %s", exc.response.status_code)
        return ToolError(error=f"LiteAPI error: HTTP {exc.response.status_code}", code="LITEAPI_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in search_hotels")
        return ToolError(error=f"Unexpected error: {exc}", code="UNKNOWN_ERROR")
    if on_offer is None:
        return ToolError(error=f"Could not find a place called {input.destination}.", code="NOT_FOUND")

    # --- this traveller's budget, applied to the shared answer ---
    hotels = [h for h in on_offer if h.price_per_night_inr <= input.budget_per_night]
    if not hotels:
        cheapest = min((h.price_per_night_inr for h in on_offer), default=None)
        hint = f" The cheapest available is ₹{cheapest:,.0f}/night." if cheapest else ""
        return ToolError(
            error=f"No hotels within ₹{input.budget_per_night:,.0f}/night in {input.destination}.{hint}",
            code="NO_RESULTS",
        )
    return hotels


def _hotels_on_offer(input: HotelSearchInput, nights: int) -> list[Hotel] | None:
    """The cheapest hotels LiteAPI has for this stay, whatever the budget; None when the place is not known.

    Raises httpx errors, RateLimited and OutsideCoverage. Cached for 15 minutes
    under what LiteAPI is asked — the place, the dates and the guests. The
    nightly budget is not part of it (see _flights_on_offer): it depends on what
    the flights cost, and is different on every re-plan of the same stay.
    """
    asked = {
        "destination": input.destination.strip(),
        "check_in": input.check_in,
        "check_out": input.check_out,
        "guests": input.guests,
    }
    cache_key = make_cache_key("hotels", asked)
    with single_flight(cache_key):
        cached = get_cached_sync(cache_key)
        if cached is not None:
            return [Hotel(**h) for h in cached]

        rooms, odd_guest = divmod(input.guests, 2)
        request = {
            "checkin": input.check_in,
            "checkout": input.check_out,
            "currency": "INR",
            "guestNationality": COUNTRY_CODE,
            "occupancies": [{"adults": 2}] * rooms + [{"adults": 1}] * odd_guest,
            "includeHotelData": True,
            "limit": 30,
        }

        def search(location: dict) -> list[Hotel]:
            resp = send(
                "liteapi",
                lambda: httpx.post(
                    f"{LITEAPI_URL}/hotels/rates",
                    headers={"X-API-Key": mcp_settings.LITEAPI_API_KEY, "Accept": "application/json"},
                    json={**request, **location},
                    timeout=45,
                ),
            )
            body = resp.json()
            info_by_id = {h["id"]: h for h in body.get("hotels") or []}
            return [
                hotel
                for item in body.get("data") or []
                if item["hotelId"] in info_by_id
                and (
                    hotel := _to_hotel(
                        info_by_id[item["hotelId"]], item.get("roomTypes") or [], nights, input.destination
                    )
                )
            ]

        # LiteAPI's own city match is exact for cities. A region ("Kerala") matches
        # no city, so fall back to a radius around its geocoded centre.
        found = search({"cityName": asked["destination"], "countryCode": COUNTRY_CODE})
        if not found:
            coords = _geocode(input.destination)
            if coords is None:
                return None
            found = search({"latitude": coords[0], "longitude": coords[1], "radius": HOTEL_SEARCH_RADIUS_M})

        hotels = sorted(found, key=lambda h: h.price_per_night_inr)[:MAX_HOTEL_RESULTS]
        if hotels:
            set_cached_sync(cache_key, [h.model_dump() for h in hotels], TTL_HOTELS)
        return hotels


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

    # "v3": Phase 25 changed what a search returns (lesser-known places fill up an interest, every
    # place says which interest it was found under). Entries cached before it would otherwise be
    # served for another 6 hours as if they were current; under a new key they just expire.
    cache_key = make_cache_key("attractions:v3", input.model_dump())
    try:
        with single_flight(cache_key):
            return _find_attractions(input, cache_key)
    except OutsideCoverage as exc:
        return ToolError(error=str(exc), code="OUTSIDE_COVERAGE")
    except RateLimited as exc:
        return _rate_limited(exc)
    except httpx.HTTPStatusError as exc:
        # str(exc) contains the request URL, API key included — report the status only.
        logger.error("OpenTripMap error: HTTP %s", exc.response.status_code)
        return ToolError(error=f"OpenTripMap API error: HTTP {exc.response.status_code}", code="OTM_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in get_attractions")
        return ToolError(error=f"Unexpected error: {exc}", code="UNKNOWN_ERROR")


def _find_attractions(input: AttractionInput, cache_key: str) -> list[Attraction] | ToolError:
    """The attractions for this search, from the cache or OpenTripMap. Raises httpx errors, RateLimited, OutsideCoverage."""
    cached = get_cached_sync(cache_key)
    if cached is not None:
        return [Attraction(**a) for a in cached]

    # Step 1 — one search per interest. Results come back nearest-first, so a
    # single combined query would fill up with whatever is closest to the
    # centre and crowd out the other interests.
    searches: dict[str, list[str]] = {}  # kinds → the interests asked for that are searched as them
    for interest in input.interests:
        if kinds := _interest_to_otm_kinds(interest):
            searches.setdefault(kinds, []).append(interest)
    if not searches:
        if input.interests:  # every one of them is something the provider has no places for
            return ToolError(
                error=(
                    f"No attractions found for {input.interests} in {input.destination}: "
                    "the attractions provider lists no places of this kind."
                ),
                code="NO_RESULTS",
            )
        searches = {"interesting_places": []}
    per_search = -(-input.limit // len(searches))  # ceil

    # Step 2 — geocode destination name to lat/lon
    coords = _geocode(input.destination)
    if coords is None:
        return ToolError(error=f"Could not find a place called {input.destination}.", code="NOT_FOUND")
    lat, lon = coords

    by_name: dict[str, Attraction] = {}  # the same place can match two interests
    columns: list[list[Attraction]] = []  # what each search added, in its own order
    for kinds, interests in searches.items():
        places = _places_near(lat, lon, kinds, per_search, WELL_KNOWN)
        if len(places) < per_search:
            # Phase 25: too few well-known ones — fill up with lesser-known ones, after them.
            names = {name for name, _ in places}
            more = _places_near(lat, lon, kinds, per_search + len(places), ANY_RATED)
            places += [(name, place) for name, place in more if name not in names][: per_search - len(places)]

        column: list[Attraction] = []
        for name, place in places:
            attraction = by_name.get(name)
            if attraction is None:
                category = _otm_kind_to_category(place.get("kinds", ""))
                # OpenTripMap's popularity rate: 1–3, or 5–7 for the same scale on a
                # heritage site. 0 = unrated — never a made-up score.
                rating = float(place.get("rate") or 0)
                known = "A popular" if rating % 4 >= WELL_KNOWN else "A lesser-known"  # 5 is a heritage site's 1
                attraction = by_name[name] = Attraction(
                    name=name,
                    category=category,
                    rating=rating,
                    description=f"{known} {category} attraction in {input.destination}.",
                    lat=place["point"]["lat"],
                    lng=place["point"]["lon"],
                )
                column.append(attraction)
            attraction.interests.extend(i for i in interests if i not in attraction.interests)
        columns.append(column)

    if not by_name:
        return ToolError(
            error=f"No attractions found for {input.interests} in {input.destination}.",
            code="NO_RESULTS",
        )

    # One from each interest in turn, so the cut at `limit` falls on all of them alike.
    attractions = [a for row in zip_longest(*columns) for a in row if a is not None][: input.limit]
    set_cached_sync(cache_key, [a.model_dump() for a in attractions], TTL_ATTRACTIONS)
    return attractions


def _places_near(lat: float, lon: float, kinds: str, limit: int, rate: int) -> list[tuple[str, dict]]:
    """(name, place) of up to `limit` places of these kinds around a point. Raises httpx errors and RateLimited.

    The builder can only schedule places it can name, and the map (Phase 18)
    can only pin places with coordinates — every place returned has both.
    """
    resp = send(
        "opentripmap",
        lambda: httpx.get(
            "https://api.opentripmap.com/0.1/en/places/radius",
            params={
                "radius": ATTRACTION_RADIUS_M,
                "lon": lon,
                "lat": lat,
                "kinds": kinds,
                "limit": limit,
                "rate": rate,
                "format": "json",
                "apikey": mcp_settings.OPENTRIPMAP_API_KEY,
            },
            timeout=10,
        ),
    )
    found: dict[str, dict] = {}
    for place in resp.json():
        name = (place.get("name") or "").strip()
        point = place.get("point") or {}
        if name and point.get("lat") is not None and point.get("lon") is not None:
            found.setdefault(name, place)
    return list(found.items())


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
    with single_flight(cache_key):
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
            resp = send(
                "openweather",
                lambda: httpx.get(
                    "https://api.openweathermap.org/data/2.5/forecast",
                    params={
                        "q": f"{input.destination},IN",
                        "appid": mcp_settings.OPENWEATHER_API_KEY,
                        "units": "metric",
                        "cnt": 40,
                    },
                    timeout=10,
                ),
            )
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

        except RateLimited as exc:
            return _rate_limited(exc)
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
    Phase 21: given a destination and a month, the estimate knows the season —
    how far the prices may move before they are booked (total_min / total_max)
    and what the same trip costs in the off-season (src/ai/pricing.py).
    Phase 25: `group_size` travellers share it — `per_person` is one share, and
    for more than one traveller `per_person_breakdown` says what it is made of.
    """
    if input.flights < 0:
        return ToolError(error="flights must be ≥ 0.", code="INVALID_INPUT")
    if input.hotels < 0:
        return ToolError(error="hotels must be ≥ 0.", code="INVALID_INPUT")
    if input.daily_spend < 0:
        return ToolError(error="daily_spend must be ≥ 0.", code="INVALID_INPUT")

    estimate = pricing.estimate(
        flights=input.flights,
        nightly=input.hotels,
        nights=input.days if input.nights is None else input.nights,
        daily=input.daily_spend,
        days=input.days,
        destination=input.destination,
        month=input.month,
    )
    season = estimate.season
    notes = (
        "Budget is within typical range."
        if estimate.total < 80_000
        else "Budget is above ₹80,000 — consider cheaper alternatives."
    )
    if season is not None:
        notes = f"{notes} {season.describe(input.destination or '')}"

    split = pricing.per_person(
        total=estimate.total,
        flights=estimate.flights,
        stay=estimate.stay,
        activities=estimate.activities,
        travellers=input.group_size,
    )
    return BudgetEstimate(
        flights=input.flights,
        hotels=estimate.stay,
        activities_estimate=estimate.activities,
        total=estimate.total,
        per_person=split["total"],
        per_person_breakdown=split if input.group_size > 1 else None,
        notes=notes,
        total_min=estimate.total_min,
        total_max=estimate.total_max,
        season=season.label if season else None,
        season_multiplier=season.multiplier if season else 1.0,
        off_peak_months=list(season.off_peak_months) if season else [],
        off_peak_total=estimate.off_peak_total,
    )

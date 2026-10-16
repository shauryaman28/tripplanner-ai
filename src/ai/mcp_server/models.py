"""Typed I/O models for all 5 MCP tools.

lat/lng added to Attraction in Phase 3 so Phase 18 map view
can use server-side coordinates instead of client-side geocoding.
Phase 18 also puts coordinates on Hotel and the two airports on Flight.
"""

from pydantic import BaseModel, Field

from src.ai.itinerary import PerPersonBreakdown

# ── Inputs ─────────────────────────────────────────────────────────────────


class FlightSearchInput(BaseModel):
    origin: str = Field(..., description="IATA airport code or known city name, e.g. DEL")
    destination: str = Field(..., description="IATA airport code or known city name, e.g. GOI or Goa")
    date: str = Field(..., description="Departure date ISO 8601, e.g. 2025-12-10")
    return_date: str | None = Field(None, description="Return date for round-trip")
    budget: float = Field(..., description="Max total price in INR (all passengers); dearer offers are dropped")
    passengers: int = Field(1, ge=1, le=9)
    max_stops: int = Field(1, ge=0, le=2, description="Max connections per direction")
    preferred_airlines: list[str] | None = Field(
        None, description="IATA carrier codes ranked first (soft preference, not a filter), e.g. ['6E', 'AI']"
    )


class HotelSearchInput(BaseModel):
    destination: str = Field(..., description="City name, e.g. Goa")
    check_in: str = Field(..., description="ISO 8601 date")
    check_out: str = Field(..., description="ISO 8601 date")
    budget_per_night: float = Field(..., description="Max price per night in INR for the whole party")
    guests: int = Field(1, ge=1, le=9)


class AttractionInput(BaseModel):
    destination: str
    interests: list[str] = Field(..., description="e.g. ['history', 'street food']")
    limit: int = Field(5, ge=1, le=20)


class WeatherInput(BaseModel):
    destination: str
    date_range: str = Field(..., description="e.g. '2025-12-10 to 2025-12-17'")


class BudgetInput(BaseModel):
    flights: float = Field(..., description="Total flight cost in INR")
    hotels: float = Field(..., description="Hotel cost per night in INR")
    days: int = Field(..., ge=1)
    daily_spend: float = Field(..., description="Estimated daily spend in INR")
    # Phase 21 — the season, and what the range is
    nights: int | None = Field(None, ge=0, description="Hotel nights; defaults to days")
    # Phase 25 — the costs above are the whole party's; this is how many people share them
    group_size: int = Field(
        1, ge=1, le=9, description="Travellers sharing the cost; flights, hotels and spend are for all of them"
    )
    destination: str | None = Field(None, description="Where the trip goes, e.g. Goa — sets its seasons")
    month: int | None = Field(None, ge=1, le=12, description="Month of travel, 1–12: the prices are this month's")


# ── Outputs ────────────────────────────────────────────────────────────────


class Airport(BaseModel):
    code: str  # IATA
    name: str | None = None
    lat: float | None = None
    lng: float | None = None


class Flight(BaseModel):
    airline: str
    flight_number: str
    departure: str
    arrival: str
    duration_mins: int
    price_inr: float
    stops: int
    origin: Airport | None = None  # Phase 18 — map info markers
    destination: Airport | None = None


class Hotel(BaseModel):
    name: str
    stars: int
    price_per_night_inr: float  # whole party, all rooms
    rating: float  # guest review score, 0–10 (0 = no reviews)
    address: str
    lat: float | None = None  # for the Phase 18 map view
    lng: float | None = None


class Attraction(BaseModel):
    name: str
    category: str
    rating: float
    description: str
    lat: float | None = None  # Phase 3 — used by Phase 18 map view
    lng: float | None = None
    # Phase 25 — which of the interests asked for this place was found under. A group's
    # attractions are told apart by it: whose interests each one answers.
    interests: list[str] = Field(default_factory=list)


class DayForecast(BaseModel):
    date: str
    condition: str
    temp_high_c: float
    temp_low_c: float


class BudgetEstimate(BaseModel):
    flights: float
    hotels: float
    activities_estimate: float
    total: float
    per_person: float  # total / group_size
    # Phase 25: each traveller's part of the flights, the stay and the activities, and their shares
    # in whole rupees. None for one traveller — there is nothing to split.
    per_person_breakdown: PerPersonBreakdown | None = None
    notes: str
    # Phase 21: where the total will likely land once booked (±10–20% by season), and the season itself
    total_min: float
    total_max: float
    season: str | None = None  # "peak" | "shoulder" | "off-peak"; None without a month
    season_multiplier: float = 1.0  # this month's prices against the destination's cheapest month
    off_peak_months: list[int] = Field(default_factory=list)
    off_peak_total: float | None = None  # the same trip in the off-season; None when it is the off-season


# ── Errors ─────────────────────────────────────────────────────────────────


class ToolError(BaseModel):
    error: str
    code: str
    # Phase 33: a flight search with nothing within the budget says what the cheapest flight did cost. Without
    # it the budget check knew no fare, and offered to raise the budget by a blind 25% — a way out that
    # led back into the same conflict (the eval suite's conflict-kochi, docs/phase33_build_log.md).
    cheapest_inr: float | None = None


# Phase 29 — besides its answer, the tool server tells its caller what its cache did for the call:
# one of the protocol's own log messages, sent under this logger name, with
# {"lookups": {"flights": {"hit": 1}, "geocode": {"hit": 2}}} as its data. The caller counts them
# (mcp_cache_lookups_total): the cache is in the tool server's process, and the metrics are read
# from the caller's.
CACHE_REPORT = "tripplanner.cache"

# The tools, and the caches they keep (the name in a cache key: mcp:<cache>[:version]:<digest>) — listed
# for the caller, which counts calls and lookups by these names and wants each counter to exist before
# its first event (src/telemetry/metrics.py says why). A test holds both lists to what the server has.
TOOL_NAMES = ("search_flights", "search_hotels", "get_attractions", "get_weather", "estimate_budget")
CACHES = ("flights", "hotels", "attractions", "weather", "geocode")

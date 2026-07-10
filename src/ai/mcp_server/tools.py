"""
Phase 2 — all 5 MCP tools with realistic mocked responses.

Each tool:
- Has typed Pydantic inputs/outputs
- Returns realistic hardcoded JSON (not TODO)
- Has at least one deliberate error case
- get_attractions is also registered as a Resource (see server.py Phase 2 note)
"""

from datetime import date

from src.ai.mcp_server.models import (
    Attraction,
    BudgetEstimate,
    BudgetInput,
    AttractionInput,
    DayForecast,
    Flight,
    FlightSearchInput,
    Hotel,
    HotelSearchInput,
    ToolError,
    WeatherInput,
)


def search_flights(input: FlightSearchInput) -> list[Flight] | ToolError:
    """Search available flights. Returns ToolError for past dates or no results."""
    if date.fromisoformat(input.date) < date.today():
        return ToolError(
            error="Departure date is in the past.",
            code="PAST_DATE",
        )

    if input.budget < 2000:
        return ToolError(
            error="Budget too low — minimum viable flight budget is ₹2,000.",
            code="BUDGET_TOO_LOW",
        )

    return [
        Flight(
            airline="IndiGo",
            flight_number="6E-204",
            departure=f"{input.date}T06:00:00",
            arrival=f"{input.date}T08:15:00",
            duration_mins=135,
            price_inr=4_200.0 * input.passengers,
            stops=0,
        ),
        Flight(
            airline="Air India",
            flight_number="AI-102",
            departure=f"{input.date}T09:30:00",
            arrival=f"{input.date}T12:00:00",
            duration_mins=150,
            price_inr=5_800.0 * input.passengers,
            stops=0,
        ),
        Flight(
            airline="SpiceJet",
            flight_number="SG-415",
            departure=f"{input.date}T14:00:00",
            arrival=f"{input.date}T17:30:00",
            duration_mins=210,
            price_inr=3_100.0 * input.passengers,
            stops=1,
        ),
    ]


def search_hotels(input: HotelSearchInput) -> list[Hotel] | ToolError:
    """Search hotels. Returns ToolError if check-out is before check-in."""
    if date.fromisoformat(input.check_out) <= date.fromisoformat(input.check_in):
        return ToolError(
            error="check_out must be after check_in.",
            code="INVALID_DATES",
        )

    return [
        Hotel(
            name=f"The {input.destination} Grand",
            stars=4,
            price_per_night_inr=min(input.budget_per_night, 4_500.0),
            rating=4.3,
            address=f"Beach Road, {input.destination}",
        ),
        Hotel(
            name="Zostel",
            stars=2,
            price_per_night_inr=min(input.budget_per_night, 1_200.0),
            rating=4.1,
            address=f"Backpacker Lane, {input.destination}",
        ),
        Hotel(
            name=f"{input.destination} Marriott",
            stars=5,
            price_per_night_inr=min(input.budget_per_night, 9_800.0),
            rating=4.7,
            address=f"Luxury Avenue, {input.destination}",
        ),
    ]


def get_attractions(input: AttractionInput) -> list[Attraction] | ToolError:
    """
    Return top attractions. Also registered as a Resource in Phase 2 (see server.py).
    Returns ToolError if limit is unreasonably high for mock data.
    """
    if input.limit > 10:
        return ToolError(
            error="Mock server only supports up to 10 results.",
            code="LIMIT_EXCEEDED",
        )

    mock_pool = [
        Attraction(name="Fort Aguada", category="history", rating=4.5, description="17th-century Portuguese fort with lighthouse."),
        Attraction(name="Anjuna Flea Market", category="street food", rating=4.2, description="Weekly market with local food and crafts."),
        Attraction(name="Dudhsagar Falls", category="nature", rating=4.6, description="Four-tiered waterfall on the Mandovi river."),
        Attraction(name="Basilica of Bom Jesus", category="history", rating=4.7, description="UNESCO World Heritage baroque church."),
        Attraction(name="Calangute Beach", category="beach", rating=4.0, description="Most popular beach in north Goa."),
        Attraction(name="Spice Plantation Tour", category="food", rating=4.4, description="Guided tour through spice gardens."),
        Attraction(name="Palolem Beach", category="beach", rating=4.6, description="Crescent-shaped beach in south Goa."),
        Attraction(name="Saturday Night Market", category="street food", rating=4.3, description="Night market with live music and local cuisine."),
        Attraction(name="Reis Magos Fort", category="history", rating=4.1, description="Laterite fort overlooking the Mandovi river."),
        Attraction(name="Dolphin Watching", category="adventure", rating=4.5, description="Boat trip to spot spinner dolphins."),
    ]

    # Filter by interests if specified; fall back to full pool
    filtered = [a for a in mock_pool if a.category in input.interests] if input.interests else mock_pool
    if not filtered:
        filtered = mock_pool

    return filtered[: input.limit]


def get_weather(input: WeatherInput) -> list[DayForecast] | ToolError:
    """Return 7-day mock forecast. Returns ToolError for unknown destinations."""
    if not input.destination.strip():
        return ToolError(
            error="Destination cannot be empty.",
            code="MISSING_DESTINATION",
        )

    # Fake 7-day forecast
    conditions = ["Sunny", "Partly Cloudy", "Sunny", "Sunny", "Cloudy", "Sunny", "Sunny"]
    return [
        DayForecast(
            date=f"Day {i + 1}",
            condition=conditions[i],
            temp_high_c=31.0 - i * 0.3,
            temp_low_c=24.0 - i * 0.2,
        )
        for i in range(7)
    ]


def estimate_budget(input: BudgetInput) -> BudgetEstimate:
    """Pure logic — no mock needed. Calculates full trip budget breakdown."""
    hotel_total = input.hotels * input.days
    activities = input.daily_spend * input.days
    total = input.flights + hotel_total + activities

    return BudgetEstimate(
        flights=input.flights,
        hotels=hotel_total,
        activities_estimate=activities,
        total=total,
        per_person=total,  # Phase 25 will make this per-person aware
        notes=(
            "Budget is within typical range."
            if total < 80_000
            else "Budget is above ₹80,000 — consider cheaper alternatives."
        ),
    )

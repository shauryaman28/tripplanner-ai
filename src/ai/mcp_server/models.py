"""Typed I/O models for all MCP tools."""

from pydantic import BaseModel, Field


# ── Inputs ─────────────────────────────────────────────────────────────────

class FlightSearchInput(BaseModel):
    origin: str = Field(..., description="IATA airport code, e.g. DEL")
    destination: str = Field(..., description="IATA airport code, e.g. BOM")
    date: str = Field(..., description="Departure date ISO 8601, e.g. 2025-12-10")
    return_date: str | None = Field(None, description="Return date for round-trip")
    budget: float = Field(..., description="Max total price in INR")
    passengers: int = Field(1, ge=1, le=9)


class HotelSearchInput(BaseModel):
    destination: str = Field(..., description="City name, e.g. Goa")
    check_in: str = Field(..., description="ISO 8601 date")
    check_out: str = Field(..., description="ISO 8601 date")
    budget_per_night: float = Field(..., description="Max price per night in INR")
    guests: int = Field(1, ge=1)


class AttractionInput(BaseModel):
    destination: str
    interests: list[str] = Field(..., description="e.g. ['history', 'street food']")
    limit: int = Field(5, ge=1, le=20)


class WeatherInput(BaseModel):
    destination: str
    date_range: str = Field(..., description="e.g. '2025-12-10 to 2025-12-17'")


class BudgetInput(BaseModel):
    flights: float = Field(..., description="Total flight cost in INR")
    hotels: float = Field(..., description="Total hotel cost in INR")
    days: int = Field(..., ge=1)
    daily_spend: float = Field(..., description="Estimated daily spend in INR")


# ── Outputs ────────────────────────────────────────────────────────────────

class Flight(BaseModel):
    airline: str
    flight_number: str
    departure: str
    arrival: str
    duration_mins: int
    price_inr: float
    stops: int


class Hotel(BaseModel):
    name: str
    stars: int
    price_per_night_inr: float
    rating: float
    address: str


class Attraction(BaseModel):
    name: str
    category: str
    rating: float
    description: str


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
    per_person: float
    notes: str


# ── Errors ─────────────────────────────────────────────────────────────────

class ToolError(BaseModel):
    error: str
    code: str

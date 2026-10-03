import uuid
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.schemas.types import UTCDateTime

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# The searches return at most ten attractions and the builder writes every day in one reply:
# past two weeks a plan is mostly empty days, and a year-long one came back as a single day.
MAX_TRIP_NIGHTS = 14


class TripCreate(BaseModel):
    destination: Annotated[NonBlank, Field(max_length=200)]
    start_date: date
    end_date: date
    budget: float = Field(gt=0)
    group_size: int = Field(default=1, ge=1, le=9)  # 9 = the flight search's passenger limit
    interests: list[NonBlank] | None = None

    @field_validator("start_date")
    @classmethod
    def start_not_in_past(cls, v: date) -> date:
        if v < date.today():
            raise ValueError("start_date cannot be in the past")
        return v

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v: date, info) -> date:
        if "start_date" in info.data and v <= info.data["start_date"]:
            raise ValueError("end_date must be after start_date")
        if "start_date" in info.data and (v - info.data["start_date"]).days > MAX_TRIP_NIGHTS:
            raise ValueError(f"a trip can be at most {MAX_TRIP_NIGHTS} nights long")
        return v


class TripRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    destination: str
    start_date: date
    end_date: date
    budget: float
    group_size: int
    interests: list[str] | None
    status: str
    created_at: UTCDateTime

    model_config = {"from_attributes": True}


class PlanRequest(BaseModel):
    """Optional body for POST /trips/{id}/plan."""

    raw_input: str | None = None


class ClarifyRequest(BaseModel):
    """Body for POST /trips/{id}/clarify."""

    answer: NonBlank


class RefineRequest(BaseModel):
    """Body for POST /trips/{id}/refine — Phase 15."""

    message: NonBlank


class ReplanRequest(BaseModel):
    """Body for POST /trips/{id}/replan — Phase 10.

    choice meanings:
      cheaper_flights  → re-run flight search with a reduced budget cap and one more stop
      reduce_days      → shorten trip by 2 days (lower hotel cost)
      increase_budget  → raise the budget to what the flights need (at least +25%) and retry
    """

    choice: Literal["cheaper_flights", "reduce_days", "increase_budget"]


class RetryRequest(BaseModel):
    """Optional body for POST /trips/{id}/retry — Phase 20.

    agent: the search to run again on a trip that has a plan (the plan keeps everything else).
    Left out — or when the trip has no plan yet — the whole trip is planned again.
    """

    agent: Literal["flight_agent", "hotel_agent", "activities_agent"] | None = None

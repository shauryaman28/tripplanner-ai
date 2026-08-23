import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, field_validator


class TripCreate(BaseModel):
    destination: str
    start_date: date
    end_date: date
    budget: float
    group_size: int = 1
    interests: list[str] | None = None

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v: date, info) -> date:
        if "start_date" in info.data and v <= info.data["start_date"]:
            raise ValueError("end_date must be after start_date")
        return v

    @field_validator("budget")
    @classmethod
    def budget_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("budget must be positive")
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
    created_at: datetime

    model_config = {"from_attributes": True}


class PlanRequest(BaseModel):
    """Optional body for POST /trips/{id}/plan."""
    raw_input: str | None = None


class ClarifyRequest(BaseModel):
    """Body for POST /trips/{id}/clarify."""
    answer: str


class ReplanRequest(BaseModel):
    """Body for POST /trips/{id}/replan — Phase 10.

    choice meanings:
      cheaper_flights  → re-run flight search with reduced budget cap
      reduce_days      → shorten trip by 2 days (lower hotel cost)
      increase_budget  → bump total budget by 25% and retry
    """
    choice: Literal["cheaper_flights", "reduce_days", "increase_budget"]

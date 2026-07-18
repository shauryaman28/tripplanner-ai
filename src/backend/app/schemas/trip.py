import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, field_validator


class TripCreate(BaseModel):
    destination: str
    start_date: date
    end_date: date
    budget: float
    group_size: int = 1
    interests: Optional[list[str]] = None

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
    interests: Optional[list[str]]
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}

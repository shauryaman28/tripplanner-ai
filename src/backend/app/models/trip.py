"""Trip table — one row per planning request.

status lifecycle:  pending → planning → completed | failed
interests stored as JSONB list of strings.
"""

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class TripStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    COMPLETED = "completed"
    FAILED = "failed"


class Trip(SQLModel, table=True):
    __tablename__ = "trips"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)

    destination: str = Field(max_length=200)
    start_date: date
    end_date: date
    budget: float = Field(gt=0)
    group_size: int = Field(default=1, ge=1)
    interests: Optional[list] = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    status: str = Field(default=TripStatus.PENDING, max_length=20)
    created_at: datetime = Field(default_factory=datetime.utcnow)

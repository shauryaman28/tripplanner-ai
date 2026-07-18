"""Itinerary table.

content       — full markdown or prose (used by PDF export, Phase 19)
structured_data — day-by-day JSON produced by ItineraryBuilder (Phase 12)
                  rendered directly by the frontend card view (Phase 17)
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class Itinerary(SQLModel, table=True):
    __tablename__ = "itineraries"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    trip_id: uuid.UUID = Field(foreign_key="trips.id", index=True)

    content: Optional[str] = Field(default=None)                  # markdown prose
    structured_data: Optional[dict] = Field(                       # machine-readable JSON
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    total_cost: Optional[float] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)

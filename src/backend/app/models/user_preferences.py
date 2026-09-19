"""UserPreferences table — one row per user (Phase 16).

user_id is the primary key: a user has at most one preference row, so there
is no surrogate id and no separate uniqueness constraint to maintain.

Lists are JSONB (same choice as trips.interests). travel_style is a plain
string in the model — SQLModel cannot map a Literal to a column type — and
is enforced by the schema layer (Literal) plus a CHECK constraint.

updated_at is refreshed automatically by SQLAlchemy on every UPDATE, so
neither the PUT route nor the PreferenceExtractor has to remember to set it.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal, get_args

from sqlalchemy import CheckConstraint, Column, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

TravelStyle = Literal["budget", "mid-range", "luxury"]
TRAVEL_STYLES: tuple[str, ...] = get_args(TravelStyle)  # ordered cheapest → most expensive


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UserPreferences(SQLModel, table=True):
    __tablename__ = "user_preferences"
    __table_args__ = (
        CheckConstraint(
            "travel_style IN ('budget', 'mid-range', 'luxury')",
            name="ck_user_preferences_travel_style",
        ),
    )

    user_id: uuid.UUID = Field(foreign_key="users.id", primary_key=True)

    dietary_restrictions: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    preferred_airlines: list[str] = Field(  # IATA carrier codes, e.g. ["6E", "AI"]
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    travel_style: str | None = Field(default=None, max_length=20)
    home_city: str | None = Field(default=None, max_length=100)

    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})

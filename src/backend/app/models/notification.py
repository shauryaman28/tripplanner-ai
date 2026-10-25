"""Notification table — one row per email a trip was due (Phase 40, migration 009).

Unique by (trip_id, kind, about): the row is written before the email is sent, and a second backend that tries
to write it too finds it there — so a trip is never sent the same email twice.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

KINDS = ("itinerary_ready", "trip_reminder")
STATUSES = ("sending", "sent", "logged", "off", "failed")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("trip_id", "kind", "about", name="uq_notifications_once"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", ondelete="CASCADE")
    trip_id: uuid.UUID = Field(foreign_key="trips.id", index=True, ondelete="CASCADE")
    kind: str = Field(max_length=20)
    about: str = Field(max_length=40)  # "first" for the itinerary; the start date a reminder was for
    status: str = Field(max_length=10)
    channel: str | None = Field(default=None, max_length=10)  # "sendgrid", or "log" in development
    error: str | None = Field(default=None, max_length=300)
    created_at: datetime = Field(default_factory=_now)
    sent_at: datetime | None = None

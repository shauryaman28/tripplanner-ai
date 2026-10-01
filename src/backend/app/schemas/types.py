"""Shared field types for response schemas."""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator


def as_utc(value: datetime) -> datetime:
    """The database stores naive UTC; label it so clients don't read it as local time."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# Serialises as ISO 8601 with an explicit UTC marker, e.g. "2026-12-10T06:00:00Z".
UTCDateTime = Annotated[datetime, AfterValidator(as_utc)]

"""Refresh tokens (Phase 35): what lets a sign-in outlast the hour its access token is good for.

A refresh token is a random string the client keeps; this table keeps only its SHA-256, so a copy of
the table signs nobody in. Each is used once: POST /auth/refresh marks it used and hands out the next,
in the same `family` — one family per sign-in. A token presented a second time, after the moment two
tabs might both have asked, means two parties have it: the whole family is revoked and both must sign
in again (#318). A family also ends at `session_ends_at`, thirty days after the sign-in, however often
it was refreshed.
"""

import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RefreshToken(SQLModel, table=True):
    __tablename__ = "refresh_tokens"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    family_id: uuid.UUID = Field(index=True)  # one sign-in, and every token refreshed from it
    token_hash: str = Field(max_length=64, unique=True, index=True)  # SHA-256 of the token, hex
    created_at: datetime = Field(default_factory=_now)
    expires_at: datetime  # this token's own end: REFRESH_TOKEN_DAYS, never past session_ends_at
    session_ends_at: datetime  # the sign-in's end: REFRESH_SESSION_DAYS after it, whatever happens between
    used_at: datetime | None = None  # when it was exchanged for the next one
    revoked_at: datetime | None = None  # signed out, or found used twice

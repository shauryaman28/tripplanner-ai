"""Signed in for longer than an hour: refresh tokens, used once each (Phase 35).

    start(user)    at sign-in: a new family, and its first token
    rotate(token)  POST /auth/refresh: the token is used up and the next one handed out — same family
    end(token)     POST /auth/logout: the family revoked, every token of it

A refresh token is 48 random bytes, URL-safe; only its SHA-256 is stored (models/refresh_token.py).

**Used twice.** A token is claimed by one UPDATE that sets `used_at` only where it is still NULL, so
two requests cannot both believe they used it first. A token found used already is one of two things:
the same browser asking twice at once — two tabs whose access tokens ran out together, both sending the
one cookie — or two parties holding it, one of whom stole it. The first happens within moments, so a
token used less than REFRESH_REUSE_GRACE_S ago is exchanged again (the tab gets a token of its own,
the family goes on). Later than that, nobody can tell the thief from the traveller: the family is
revoked, both have to sign in again, and the log says so.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.config import settings
from app.models.refresh_token import RefreshToken

logger = logging.getLogger(__name__)

TOKEN_BYTES = 48


class SessionEnded(Exception):
    """The refresh token cannot be used: unknown, expired, signed out, or used twice. `reason` is for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Issued:
    user_id: uuid.UUID
    token: str  # the refresh token itself — the client's to keep, never stored here
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _issue(db: AsyncSession, user_id: uuid.UUID, family_id: uuid.UUID, session_ends_at: datetime) -> Issued:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    expires_at = min(_now() + timedelta(days=settings.REFRESH_TOKEN_DAYS), session_ends_at)
    db.add(
        RefreshToken(
            user_id=user_id,
            family_id=family_id,
            token_hash=digest(token),
            expires_at=expires_at,
            session_ends_at=session_ends_at,
        )
    )
    return Issued(user_id, token, expires_at)


async def start(db: AsyncSession, user_id: uuid.UUID) -> Issued:
    """A new sign-in: a family of its own. The user's tokens that are of no more use are cleared away —
    expired ones, and those used or revoked over a day ago (until then a token presented again is still
    recognised as used, and ends its family)."""
    now = _now()
    day_ago = now - timedelta(days=1)
    await db.execute(
        delete(RefreshToken).where(
            RefreshToken.user_id == user_id,
            or_(RefreshToken.expires_at <= now, RefreshToken.used_at < day_ago, RefreshToken.revoked_at < day_ago),
        )
    )
    issued = _issue(db, user_id, uuid.uuid4(), now + timedelta(days=settings.REFRESH_SESSION_DAYS))
    await db.commit()
    return issued


async def _row(db: AsyncSession, token: str) -> RefreshToken:
    row = (await db.execute(select(RefreshToken).where(RefreshToken.token_hash == digest(token)))).scalar_one_or_none()
    if row is None:
        raise SessionEnded("unknown token")
    return row


async def rotate(db: AsyncSession, token: str) -> Issued:
    """Exchange a refresh token for the next one. Raises SessionEnded when it cannot be."""
    row = await _row(db, token)
    now = _now()
    if row.revoked_at is not None:
        raise SessionEnded("signed out, or ended for a token used twice")
    if row.expires_at <= now:
        raise SessionEnded("expired")
    claimed = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.id == row.id, RefreshToken.used_at.is_(None))
        .values(used_at=now)
        .returning(RefreshToken.id)
    )
    if claimed.first() is None:  # used already — by this browser a moment ago, or by somebody else
        used_at = (await db.execute(select(RefreshToken.used_at).where(RefreshToken.id == row.id))).scalar_one()
        if used_at is None or (now - used_at).total_seconds() > settings.REFRESH_REUSE_GRACE_S:
            await db.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            await db.commit()
            logger.warning(
                "[AUTH] a refresh token was used again %.0f s after it was used: its sign-in is ended",
                (now - used_at).total_seconds() if used_at else -1,
            )
            raise SessionEnded("used twice")
    issued = _issue(db, row.user_id, row.family_id, row.session_ends_at)
    await db.commit()
    return issued


async def end(db: AsyncSession, token: str) -> bool:
    """Sign out: revoke the token's whole family. False when the token was not one of ours."""
    try:
        row = await _row(db, token)
    except SessionEnded:
        return False
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    await db.commit()
    return True

"""JWT creation / verification and password hashing.

Kept in core/ so it has zero FastAPI dependencies and is easy to
unit-test without spinning up any server.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt  # PyJWT (Phase 35: python-jose has an unfixed advisory and pulls in ecdsa, which has another)
from passlib.context import CryptContext

from app.core.config import settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Passwords ─────────────────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────


def create_access_token(subject: str | Any) -> str:
    """Return a signed JWT with 'sub' claim set to *subject* (user UUID)."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {"sub": str(subject), "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and verify a JWT: its signature, by the one algorithm configured, and its expiry. Raises
    jwt.InvalidTokenError (an ExpiredSignatureError when it has run out) on any failure."""
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])

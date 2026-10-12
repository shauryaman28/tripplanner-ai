"""Reusable FastAPI dependencies.

get_current_user      — Bearer token in Authorization header (standard routes)
get_current_user_sse  — Bearer token in header OR ?token= query param (SSE)
get_redis_dep         — thin wrapper so routes don't import redis directly
get_redis_or_none     — the same client, or None when Redis is not available (cache-only use)
may_read_metrics      — who GET /metrics answers (Phase 29)
"""

import hmac
import uuid

import redis.asyncio as aioredis
from fastapi import Depends, Header, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decode_access_token
from app.db.redis import get_redis
from app.db.session import get_db
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def _user_from_token(token: str, db: AsyncSession) -> User:
    """Decode a JWT and load its user. Any failure — bad signature, expired,
    malformed subject, deleted user — is the same 401."""
    try:
        user_id = uuid.UUID(decode_access_token(token).get("sub", ""))
    except (JWTError, ValueError, TypeError, AttributeError):
        raise _UNAUTHORIZED
    user = await db.get(User, user_id)
    if user is None:
        raise _UNAUTHORIZED
    return user


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Validate Bearer JWT and return the authenticated User (401 otherwise)."""
    return await _user_from_token(token, db)


async def get_current_user_sse(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Like get_current_user but also accepts token as ?token= query param.

    Browsers cannot set custom headers for EventSource connections, so the
    SSE endpoint accepts the JWT as a query parameter as a fallback.
    """
    raw = authorization[7:] if authorization and authorization.startswith("Bearer ") else token
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required (use Authorization header or ?token=)",
        )
    return await _user_from_token(raw, db)


async def get_redis_dep() -> aioredis.Redis:
    return await get_redis()


async def get_redis_or_none() -> aioredis.Redis | None:
    """For a route that uses Redis only as a cache: None when it is not there, instead of a 500."""
    try:
        return await get_redis()
    except RuntimeError:
        return None


def may_read_metrics(authorization: str | None = Header(None), x_ops_token: str | None = Header(None)) -> None:
    """Phase 29 — who GET /metrics answers.

    The numbers name every route and say how busy each is: not a secret worth much, and not for
    the whole internet either. With `OPS_TOKEN` set they are for whoever sends it — as
    `Authorization: Bearer …`, which is what Prometheus can be told to send, or as X-Ops-Token like
    the other operator's route. With none set they are open where the app is being developed and
    closed where it is deployed: a deployment that forgot the token does not publish its metrics.
    A caller who may not read them is told what a route that is not there says.
    """
    expected = settings.OPS_TOKEN
    if not expected:
        if str(settings.APP_ENV).lower() == "production":
            raise HTTPException(status_code=404, detail="Not Found")
        return
    bearer = authorization[7:] if authorization and authorization[:7].lower() == "bearer " else ""
    sent = bearer or x_ops_token or ""
    if not hmac.compare_digest(sent.encode(), expected.encode()):
        raise HTTPException(status_code=404, detail="Not Found")

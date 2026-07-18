"""Reusable FastAPI dependencies.

get_current_user      — Bearer token in Authorization header (standard routes)
get_current_user_sse  — Bearer token in header OR ?token= query param (SSE)
get_redis_dep         — thin wrapper so routes don't import redis directly
"""

import uuid
from typing import Optional

import redis.asyncio as aioredis
from fastapi import Depends, Header, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.redis import get_redis
from app.db.session import get_db
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Validate Bearer JWT and return the authenticated User.

    Raises 401 on any invalid / expired token.
    """
    _unauth = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        user_id: Optional[str] = payload.get("sub")
        if not user_id:
            raise _unauth
    except JWTError:
        raise _unauth

    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        raise _unauth
    return user


async def get_current_user_sse(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Like get_current_user but also accepts token as ?token= query param.

    Browsers cannot set custom headers for EventSource connections, so the
    SSE endpoint accepts the JWT as a query parameter as a fallback.
    """
    raw: Optional[str] = None
    if authorization and authorization.startswith("Bearer "):
        raw = authorization[7:]
    elif token:
        raw = token

    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required (use Authorization header or ?token=)",
        )

    _unauth = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    try:
        payload = decode_access_token(raw)
        user_id: Optional[str] = payload.get("sub")
        if not user_id:
            raise _unauth
    except JWTError:
        raise _unauth

    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        raise _unauth
    return user


async def get_redis_dep() -> aioredis.Redis:
    return await get_redis()

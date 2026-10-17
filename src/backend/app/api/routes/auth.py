"""
Auth routes.

POST /auth/register   { email, password }  → UserRead (201)
POST /auth/login      form(username, password) → Token (200)
POST /auth/refresh    { refresh_token }, or the refresh cookie → Token (200)       (Phase 35)
POST /auth/logout     { refresh_token }, or the refresh cookie → 204              (Phase 35)

/auth/login accepts the OAuth2 password form so it works out of the box with
FastAPI's Swagger UI 'Authorize' button.

Phase 35. An access token is good for an hour (JWT_EXPIRE_MINUTES); a sign-in lasts as long as its
refresh token keeps being exchanged (app/core/sessions.py). The refresh token is in the answer's body —
for a client that is not a browser — and in an HttpOnly cookie, which is what the site uses: a script
on the page cannot read it, and the browser sends it only to these routes. Login and registration are
limited per address (app/core/ratelimit.py).
"""

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.api.deps import get_redis_or_none
from app.core import ratelimit, sessions
from app.core.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import Token, UserCreate, UserRead
from app.schemas.text import EMAIL_CHARS

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "tp_refresh"
LOGIN_PASSWORD_CHARS = 128  # what the form takes; registration holds a password to 72 bytes (bcrypt's)
REFRESH_TOKEN_CHARS = 256  # ours are 64


class RefreshRequest(BaseModel):
    """Body for POST /auth/refresh and /auth/logout. A browser sends nothing: its cookie is the token."""

    refresh_token: str | None = Field(default=None, max_length=REFRESH_TOKEN_CHARS)


def _address(request: Request) -> str:
    """Whom an attempt is counted against: the client as uvicorn resolved it (see app/core/ratelimit.py)."""
    return request.client.host if request.client else "unknown"


async def _limited(redis: aioredis.Redis | None, action: str, request: Request, limit: int) -> None:
    verdict = await ratelimit.attempt(redis, action, _address(request), limit)
    if not verdict.allowed:
        what = "sign-in attempts" if action == "login" else "new accounts"
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many {what} from this address. Try again in {verdict.retry_after_s} seconds.",
            headers={"Retry-After": str(verdict.retry_after_s)},
        )


def _production() -> bool:
    return str(settings.APP_ENV).lower() == "production"


def _tokens(response: Response, issued: sessions.Issued) -> dict:
    """The answer to a sign-in or a refresh: both tokens in the body, the refresh token in the cookie too."""
    response.set_cookie(
        REFRESH_COOKIE,
        issued.token,
        max_age=settings.REFRESH_TOKEN_DAYS * 86_400,
        path=settings.REFRESH_COOKIE_PATH,
        httponly=True,
        secure=_production(),  # https in production; a laptop's http://localhost would never send it back
        samesite="strict",
    )
    return {
        "access_token": create_access_token(str(issued.user_id)),
        "token_type": "bearer",
        "expires_in": settings.JWT_EXPIRE_MINUTES * 60,
        "refresh_token": issued.token,
    }


def _forget_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE, path=settings.REFRESH_COOKIE_PATH, httponly=True, secure=_production(), samesite="strict"
    )


@router.post("/register", response_model=UserRead, status_code=201)
async def register(
    body: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis | None = Depends(get_redis_or_none),
) -> User:
    """Create a new user account. Returns 400 if email is already registered; 429 past REGISTRATIONS_PER_MINUTE."""
    await _limited(redis, "register", request, settings.REGISTRATIONS_PER_MINUTE)
    existing = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered.",
        )

    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=Token)
async def login(
    request: Request,
    response: Response,
    username: str = Form(..., max_length=EMAIL_CHARS),
    password: str = Form(..., max_length=LOGIN_PASSWORD_CHARS),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis | None = Depends(get_redis_or_none),
) -> dict:
    """An access token and a refresh token. `username` is the user's email address.

    429 after LOGIN_ATTEMPTS_PER_MINUTE attempts from one address in any 60 seconds — counted before
    the password is checked, so a refused attempt costs no bcrypt either.
    """
    await _limited(redis, "login", request, settings.LOGIN_ATTEMPTS_PER_MINUTE)
    user = (await db.execute(select(User).where(User.email == username))).scalar_one_or_none()

    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return _tokens(response, await sessions.start(db, user.id))


def _presented(request: Request, body: RefreshRequest | None) -> str | None:
    if body is not None and body.refresh_token:
        return body.refresh_token
    cookie = request.cookies.get(REFRESH_COOKIE)
    return cookie[:REFRESH_TOKEN_CHARS] if cookie else None


@router.post("/refresh", response_model=Token)
async def refresh(
    request: Request,
    response: Response,
    body: RefreshRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A new access token, and the next refresh token: the one presented is used up.

    401 when it cannot be — unknown, expired, signed out, or used twice (which also ends every other
    token of that sign-in). The browser's cookie is cleared then too: there is nothing left to refresh.
    """
    token = _presented(request, body)
    try:
        if not token:
            raise sessions.SessionEnded("no token")
        return _tokens(response, await sessions.rotate(db, token))
    except sessions.SessionEnded:
        ended = Response(status_code=status.HTTP_401_UNAUTHORIZED)
        _forget_cookie(ended)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has ended. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer", "set-cookie": ended.headers["set-cookie"]},
        ) from None


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    body: RefreshRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Sign out: the refresh token's whole sign-in is revoked, and the cookie cleared. 204 whatever was sent —
    signing out twice, or with nothing, is not an error. (An access token already handed out stays good for
    what is left of its hour: it is checked by its signature, not looked up.)"""
    token = _presented(request, body)
    if token:
        await sessions.end(db, token)
    done = Response(status_code=204)
    _forget_cookie(done)
    return done

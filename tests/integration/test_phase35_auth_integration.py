"""Phase 35 on the real things: refresh tokens in Postgres, the sign-in limit in Redis.

Run with:
    RUN_INTEGRATION=1 pytest tests/integration/test_phase35_auth_integration.py -v

Signing in, staying signed in, signing out, a token stolen and used, a token used twice by two tabs at
once — through the HTTP API, with the database's own rows read afterwards. The limit's Lua script on
a real Redis: five, then a refusal that says how long, nobody else affected, and room again when the
window has moved. And a destination that looks like SQL, stored as the words it is.
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, update
from sqlmodel import select

from app.core import ratelimit, sessions
from app.core.config import settings
from app.core.security import decode_access_token
from app.models.refresh_token import RefreshToken
from app.models.trip import Trip

pytestmark = [
    pytest.mark.skipif(not os.getenv("RUN_INTEGRATION"), reason="Set RUN_INTEGRATION=1 (requires Docker)"),
    pytest.mark.asyncio,
]

PASSWORD = "a-long-enough-password"


def utc_now() -> datetime:
    """As the app keeps times: UTC, without a zone (models/refresh_token.py)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@asynccontextmanager
async def api(**settings_):
    from app.db.redis import close_redis, init_redis
    from app.main import app

    await init_redis()
    try:
        with patch.multiple(settings, **{"APP_ENV": "development", **settings_}):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
                yield http
    finally:
        await close_redis()


async def sign_up_and_in(http: AsyncClient) -> tuple[str, dict]:
    email = f"p35-{uuid.uuid4().hex[:8]}@example.com"
    assert (await http.post("/auth/register", json={"email": email, "password": PASSWORD})).status_code == 201
    login = await http.post("/auth/login", data={"username": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return email, login


async def family_of(db_session, token: str) -> list[RefreshToken]:
    row = (
        await db_session.execute(select(RefreshToken).where(RefreshToken.token_hash == sessions.digest(token)))
    ).scalar_one()
    return list(
        (await db_session.execute(select(RefreshToken).where(RefreshToken.family_id == row.family_id))).scalars()
    )


# ── Signing in, and staying signed in ──────────────────────────────────────


async def test_a_sign_in_is_an_hours_access_token_and_a_refresh_token_in_the_body_and_an_httponly_cookie(db_session):
    async with api() as http:
        _, login = await sign_up_and_in(http)

    body = login.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 3600
    claims = decode_access_token(body["access_token"])
    assert 3500 < claims["exp"] - datetime.now().timestamp() <= 3600
    cookie = login.headers["set-cookie"]
    assert cookie.startswith(f"tp_refresh={body['refresh_token']};")
    for attribute in ("HttpOnly", "Max-Age=1209600", "Path=/api/auth", "SameSite=strict"):
        assert attribute in cookie, cookie
    assert "Secure" not in cookie  # a laptop's http://localhost; in production it is (below)
    # only its hash is kept: the table signs nobody in
    (row,) = await family_of(db_session, body["refresh_token"])
    assert row.token_hash == sessions.digest(body["refresh_token"]) and body["refresh_token"] not in row.token_hash
    assert row.session_ends_at - row.created_at > timedelta(days=29)


async def test_in_production_the_cookie_is_secure(db_session):
    async with api(APP_ENV="production") as http:
        _, login = await sign_up_and_in(http)
    assert "Secure" in login.headers["set-cookie"]


async def test_a_refresh_uses_the_token_up_and_hands_out_the_next_from_the_body_or_from_the_cookie(db_session):
    async with api() as http:
        _, login = await sign_up_and_in(http)
        first = login.json()["refresh_token"]
        by_body = await http.post("/auth/refresh", json={"refresh_token": first})
        assert by_body.status_code == 200, by_body.text
        second = by_body.json()["refresh_token"]
        # a browser sends nothing in the body: its cookie is the token
        http.cookies.set("tp_refresh", second)
        by_cookie = await http.post("/auth/refresh")
        assert by_cookie.status_code == 200
        third = by_cookie.json()["refresh_token"]
        # the new access token works
        trips = await http.get("/trips", headers={"Authorization": f"Bearer {by_cookie.json()['access_token']}"})
        assert trips.status_code == 200

    assert len({first, second, third}) == 3
    family = {row.token_hash: row for row in await family_of(db_session, third)}
    assert len(family) == 3
    assert family[sessions.digest(first)].used_at and family[sessions.digest(second)].used_at
    assert family[sessions.digest(third)].used_at is None
    assert len({row.session_ends_at for row in family.values()}) == 1  # one sign-in, however often refreshed


async def test_two_tabs_refreshing_at_once_both_stay_signed_in(db_session):
    """Both send the one cookie; the second finds it used a moment ago — the same browser, not a thief."""
    async with api() as http:
        _, login = await sign_up_and_in(http)
        token = login.json()["refresh_token"]
        answers = await asyncio.gather(*(http.post("/auth/refresh", json={"refresh_token": token}) for _ in range(2)))
    assert [answer.status_code for answer in answers] == [200, 200]
    assert answers[0].json()["refresh_token"] != answers[1].json()["refresh_token"]
    assert all(row.revoked_at is None for row in await family_of(db_session, token))


async def test_a_stolen_token_used_after_the_traveller_has_used_it_ends_the_sign_in_for_both(db_session, caplog):
    caplog.set_level("WARNING", logger="app.core.sessions")
    async with api(REFRESH_REUSE_GRACE_S=0) as http:
        _, login = await sign_up_and_in(http)
        stolen = login.json()["refresh_token"]
        travellers = (await http.post("/auth/refresh", json={"refresh_token": stolen})).json()["refresh_token"]
        await asyncio.sleep(0.05)
        thief = await http.post("/auth/refresh", json={"refresh_token": stolen})
        traveller_next = await http.post("/auth/refresh", json={"refresh_token": travellers})

    assert thief.status_code == 401 and thief.json()["detail"] == "Your session has ended. Please sign in again."
    assert 'tp_refresh=""' in thief.headers["set-cookie"] and "Max-Age=0" in thief.headers["set-cookie"]
    assert traveller_next.status_code == 401  # nobody can tell the thief from the traveller: both sign in again
    assert all(row.revoked_at is not None for row in await family_of(db_session, stolen))
    assert any("used again" in record.getMessage() for record in caplog.records)


async def test_signing_out_ends_the_sign_in_and_another_sign_in_is_untouched(db_session):
    async with api() as http:
        email, login = await sign_up_and_in(http)
        other = (await http.post("/auth/login", data={"username": email, "password": PASSWORD})).json()["refresh_token"]
        token = login.json()["refresh_token"]
        out = await http.post("/auth/logout", json={"refresh_token": token})
        again = await http.post("/auth/logout", json={"refresh_token": token})
        nothing = await http.post("/auth/logout")
        after = await http.post("/auth/refresh", json={"refresh_token": token})
        elsewhere = await http.post("/auth/refresh", json={"refresh_token": other})

    assert (out.status_code, again.status_code, nothing.status_code) == (204, 204, 204)
    assert 'tp_refresh=""' in out.headers["set-cookie"]
    assert after.status_code == 401
    assert elsewhere.status_code == 200  # the phone signed in separately is still signed in


async def test_an_expired_token_or_one_nobody_issued_is_refused(db_session):
    async with api() as http:
        _, login = await sign_up_and_in(http)
        token = login.json()["refresh_token"]
        await db_session.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == sessions.digest(token))
            .values(expires_at=utc_now() - timedelta(seconds=1))
        )
        await db_session.commit()
        expired = await http.post("/auth/refresh", json={"refresh_token": token})
        made_up = await http.post("/auth/refresh", json={"refresh_token": "x" * 64})
        none = await http.post("/auth/refresh")
        too_long = await http.post("/auth/refresh", json={"refresh_token": "x" * 300})
    assert (expired.status_code, made_up.status_code, none.status_code) == (401, 401, 401)
    assert too_long.status_code == 422


async def test_a_sign_in_ends_thirty_days_after_it_began_however_often_it_is_refreshed(db_session):
    async with api() as http:
        _, login = await sign_up_and_in(http)
        token = login.json()["refresh_token"]
        await db_session.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == sessions.digest(token))
            .values(session_ends_at=utc_now() + timedelta(hours=1))
        )
        await db_session.commit()
        refreshed = (await http.post("/auth/refresh", json={"refresh_token": token})).json()["refresh_token"]
    (row,) = [r for r in await family_of(db_session, token) if r.token_hash == sessions.digest(refreshed)]
    assert row.expires_at <= row.session_ends_at < utc_now() + timedelta(hours=1, minutes=1)


async def test_a_new_sign_in_clears_away_what_is_of_no_more_use(db_session):
    async with api() as http:
        email, login = await sign_up_and_in(http)
        token = login.json()["refresh_token"]
        await http.post("/auth/refresh", json={"refresh_token": token})
        await db_session.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == sessions.digest(token))
            .values(used_at=utc_now() - timedelta(days=2))
        )
        await db_session.commit()
        await http.post("/auth/login", data={"username": email, "password": PASSWORD})
    left = (await db_session.execute(select(RefreshToken.token_hash))).scalars().all()
    assert sessions.digest(token) not in left and len(left) == 2  # the live one of the first sign-in, and the new


# ── The limit on signing in, on a real Redis ───────────────────────────────


async def test_the_sixth_sign_in_attempt_within_a_minute_is_refused_and_told_how_long_to_wait(db_session):
    async with api() as http:
        email, _ = await sign_up_and_in(http)
        address = str(uuid.uuid4())  # a fresh window: the test client's own address is shared with other tests
        with (
            patch("app.api.routes.auth._address", lambda _request: address),
            patch.object(settings, "LOGIN_ATTEMPTS_PER_MINUTE", 5),
        ):
            answers = [
                await http.post("/auth/login", data={"username": email, "password": "wrong-password"}) for _ in range(5)
            ]
            sixth = await http.post("/auth/login", data={"username": email, "password": PASSWORD})
        elsewhere = await http.post("/auth/login", data={"username": email, "password": PASSWORD})  # another address
    assert [answer.status_code for answer in answers] == [401] * 5
    assert sixth.status_code == 429 and 55 <= int(sixth.headers["retry-after"]) <= 60
    assert elsewhere.status_code == 200


async def test_the_window_slides_and_a_refused_attempt_does_not_count():
    from app.db.redis import close_redis, init_redis

    redis = await init_redis()
    address = f"window-{uuid.uuid4().hex}"
    try:

        async def attempt() -> bool:
            return (await ratelimit.attempt(redis, "login", address, 3, window_s=1.0)).allowed

        # two now, one 0.6 s later: three in the window, and the fourth refused
        first = [await attempt(), await attempt()]
        await asyncio.sleep(0.6)
        assert first + [await attempt(), await attempt()] == [True, True, True, False]
        assert await redis.zcard(f"ratelimit:login:{address}") == 3  # the refused one was never added
        # 0.5 s more: the first two have left the window, the third has not — so two more, then refused again.
        # (A window that never slid would refuse all three; a key that expired whole would let all three in.)
        await asyncio.sleep(0.5)
        assert [await attempt(), await attempt(), await attempt()] == [True, True, False]
        assert 0 < await redis.pttl(f"ratelimit:login:{address}") <= 1000  # it does not outlive its window
    finally:
        await redis.delete(f"ratelimit:login:{address}")
        await close_redis()


async def test_two_attempts_at_the_same_moment_cannot_both_take_the_last_place():
    from app.db.redis import close_redis, init_redis

    redis = await init_redis()
    address = f"race-{uuid.uuid4().hex}"
    try:
        verdicts = await asyncio.gather(*(ratelimit.attempt(redis, "login", address, 5) for _ in range(20)))
        assert sum(verdict.allowed for verdict in verdicts) == 5
    finally:
        await redis.delete(f"ratelimit:login:{address}")
        await close_redis()


# ── What looks like SQL is stored as words ─────────────────────────────────


async def test_a_destination_that_looks_like_sql_is_kept_as_the_words_it_is(db_session):
    async with api() as http:
        _, login = await sign_up_and_in(http)
        token = login.json()["access_token"]
        made = await http.post(
            "/trips",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "destination": "Goa'; DROP TABLE trips;--",
                "start_date": str(datetime.now().date() + timedelta(days=40)),
                "end_date": str(datetime.now().date() + timedelta(days=43)),
                "budget": 50_000,
            },
        )
    assert made.status_code == 201, made.text
    stored = (await db_session.execute(select(Trip).where(Trip.id == uuid.UUID(made.json()["id"])))).scalar_one()
    assert stored.destination == "Goa'; DROP TABLE trips;--"
    assert (await db_session.execute(text("SELECT to_regclass('trips')"))).scalar() == "trips"

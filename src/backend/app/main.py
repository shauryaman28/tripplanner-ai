"""
FastAPI application entry point.

  uvicorn app.main:app --reload --app-dir src/backend    (local dev, from the repo root)
  docker compose up backend                              (Docker)
"""

import asyncio
import logging
import re
import uuid
from contextlib import asynccontextmanager
from http import HTTPStatus
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import func
from sqlmodel import select, update
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__, preflight
from app.api.deps import may_read_metrics
from app.api.routes.admin import router as admin_router
from app.api.routes.auth import router as auth_router
from app.api.routes.health import router as health_router
from app.api.routes.trips import INTERRUPTED, finish_runs
from app.api.routes.trips import router as trips_router
from app.api.routes.users import router as users_router
from app.core import error_reporting
from app.core.config import settings
from app.core.security import decode_access_token
from app.db import redis as redis_db
from app.db.redis import close_redis, init_redis
from app.db.session import AsyncSessionLocal
from app.db.url import where as database_at
from app.models.agent_run import AgentRun
from app.models.embedding import Embedding
from app.models.itinerary import Itinerary
from app.models.trip import Trip, TripStatus
from src.ai import tracing
from src.ai.embeddings.embedder import PENDING_RETRY_MODEL
from src.ai.mcp_client.client import close_session
from src.ai.mcp_server.config import mcp_settings
from src.ai.utils.conversation import RUN_TTL_S, run_in_flight
from src.ai.utils.embeddings import EMBEDDINGS_PENDING_KEY, generate_embeddings
from src.ai.utils.tasks import spawn
from src.telemetry import context as telemetry_context
from src.telemetry import logs, metrics, traces

logger = logging.getLogger(__name__)

# Phase 29: with LOG_FORMAT=json (the default in production) every line this process logs is one JSON
# object. Here, as the module is loaded, because the web server's own first lines come before the
# app's startup does — and a log store should not have to cope with two kinds of line.
_scrub = error_reporting.Scrubber(error_reporting.secrets_of(settings, mcp_settings)).text
logs.configure(logs.chosen(settings.LOG_FORMAT, settings.APP_ENV), traces.BACKEND, scrub=_scrub)


# ── Logging ────────────────────────────────────────────────────────────────


def _show_app_logs() -> None:
    """Send the app's INFO lines to the console, as uvicorn does its own.

    Uvicorn sets up the "uvicorn" loggers and nothing else, so a line logged at
    INFO by the app ("app.…") or the planner ("src.ai.…") went nowhere:
    "[CACHE WARM] flights: …" (Phase 24) and "[EMBEDDING RECOVERY] …" were
    written and never seen. Warnings were shown already, by Python's handler of
    last resort — which steps aside once a logger has a handler of its own.

    Only when nothing handles the root logger: under pytest, or in a deployment
    that brings its own logging config, this adds nothing.
    """
    if logging.getLogger().handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))  # uvicorn's own layout
    for name in ("app", "src"):
        app_logger = logging.getLogger(name)
        if not app_logger.handlers:
            app_logger.addHandler(handler)
            app_logger.setLevel(logging.INFO)


# ── Startup recovery ───────────────────────────────────────────────────────


RECHECK_TIMES = 20  # how often a trip another instance is planning is looked at again, RUN_TTL_S apart


async def _being_planned_elsewhere(trip_ids: list) -> set:
    """Of these trips, the ones some process is at work on right now: its note in Redis is being kept up.

    When Redis cannot be asked, none — a trip left "planning" for ever is worse than a run failed twice.
    """
    redis = redis_db.redis_client
    if redis is None or not trip_ids:
        return set()
    try:
        return {trip_id for trip_id in trip_ids if await run_in_flight(redis, str(trip_id))}
    except Exception as exc:
        logger.warning("[STARTUP RECOVERY] Redis could not say which runs are alive (%s) — taking none to be", exc)
        return set()


async def _mark_interrupted(db, trip_ids: list) -> None:
    """These trips' runs died with their process: completed if a plan stands (a refinement), failed if none does."""
    if not trip_ids:
        return
    theirs = Trip.id.in_(trip_ids), Trip.status == TripStatus.PLANNING
    has_itinerary = select(Itinerary.id).where(Itinerary.trip_id == Trip.id).exists()
    await db.execute(update(Trip).where(*theirs, has_itinerary).values(status=TripStatus.COMPLETED))
    failed = (
        (await db.execute(update(Trip).where(*theirs).values(status=TripStatus.FAILED).returning(Trip.id)))
        .scalars()
        .all()
    )
    for trip_id in failed:  # why, for the page (GET /status → failure_reason)
        turn = await db.scalar(select(func.coalesce(func.max(AgentRun.turn), 1)).where(AgentRun.trip_id == trip_id))
        db.add(
            AgentRun(
                trip_id=trip_id,
                agent_name="interrupted",
                status="failed",
                turn=turn or 1,
                input={},
                output={"reason": INTERRUPTED},
                duration_ms=0,
            )
        )
    await db.commit()


async def _recover_after_restart() -> None:
    """Repair what an unclean shutdown leaves behind.

    Planning runs and embedding jobs are in-process tasks, so none survives a
    restart:
      • a trip still "planning" was interrupted → mark it failed (Phase 13), so
        it can be planned again instead of answering 409 forever — or completed
        if it has an itinerary: an interrupted refinement leaves that plan standing;
      • the in-flight embedding counter is stale → reset it;
      • embeddings that fell back to "pending_retry" → generate them again (Phase 14),
        one itinerary after another: after migration 004 that is every trip, and the
        embedding API's free tier answers a burst with 429s.

    Phase 27: "still planning" is not proof of a dead run any more. A platform
    starts the new instance while the old one is serving, and for a while both
    are alive: the old one is still making plans, and this startup must not
    fail them under it. A run in flight keeps a note in Redis (routes/trips.py);
    a trip whose note is there is left alone and looked at again later, in case
    its process dies after all.

    Failures are logged, never raised — the app must boot even if the database
    is unreachable (GET /ping reports that in its body).
    """
    try:
        async with AsyncSessionLocal() as db:
            planning = (await db.execute(select(Trip.id).where(Trip.status == TripStatus.PLANNING))).scalars().all()
            elsewhere = await _being_planned_elsewhere(planning)
            await _mark_interrupted(db, [trip_id for trip_id in planning if trip_id not in elsewhere])
            await db.commit()
            pending = (
                (
                    await db.execute(
                        select(Embedding.itinerary_id)
                        .where(Embedding.embedding_model == PENDING_RETRY_MODEL)
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )

        if elsewhere:
            logger.info(
                "[STARTUP RECOVERY] %d trip(s) are being planned by another instance — left alone, "
                "looked at again in %d s",
                len(elsewhere),
                RUN_TTL_S,
            )
            spawn(_look_again(sorted(elsewhere, key=str)))
        if pending:
            logger.info("[EMBEDDING RECOVERY] Re-queuing %d pending embedding(s)", len(pending))
            spawn(_regenerate_embeddings(pending))
    except Exception as exc:
        logger.warning("[STARTUP RECOVERY] skipped: %s", exc)


async def _look_again(trip_ids: list) -> None:
    """The trips another instance was planning as this one started: when its note goes and the trip still
    says "planning", that instance died in the middle — and nothing else would ever notice."""
    for _ in range(RECHECK_TIMES):
        await asyncio.sleep(RUN_TTL_S)
        try:
            async with AsyncSessionLocal() as db:
                still = (
                    (await db.execute(select(Trip.id).where(Trip.id.in_(trip_ids), Trip.status == TripStatus.PLANNING)))
                    .scalars()
                    .all()
                )
                alive = await _being_planned_elsewhere(still)
                abandoned = [trip_id for trip_id in still if trip_id not in alive]
                if abandoned:
                    logger.warning("[STARTUP RECOVERY] %d run(s) died with the instance that had them", len(abandoned))
                    await _mark_interrupted(db, abandoned)
        except Exception as exc:
            logger.warning("[STARTUP RECOVERY] could not look again: %s", exc)
            continue
        trip_ids = sorted(alive, key=str)
        if not trip_ids:
            return


async def _regenerate_embeddings(itinerary_ids: list) -> None:
    """Background task: the pending embeddings, one itinerary at a time. generate_embeddings never raises."""
    for itinerary_id in itinerary_ids:
        await generate_embeddings(itinerary_id)


# ── Lifespan ───────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    _show_app_logs()
    error_reporting.quiet_access_log()  # Phase 27: a login token in an address is not written to the log
    # Phase 27: in production, refuse to serve from settings that are not fit to — and say all of it at once.
    report = preflight.enforce(settings, mcp_settings)
    for problem in report.warnings if report.production else []:
        logger.warning("[PREFLIGHT] %s", problem)
    logger.info(
        "[STARTUP] %s — database %s, Redis %s",
        settings.APP_ENV,
        database_at(settings.DATABASE_URL),
        redis_db.where(settings.REDIS_URL),
    )
    error_reporting.configure(settings, mcp_settings)  # Phase 27: Sentry — on only with a DSN
    tracing.configure(settings)  # Phase 26: LangSmith tracing — on only with the switch and a key
    # Phase 29: OpenTelemetry — on only with an endpoint; every span scrubbed like an error report before it leaves
    traces.configure(settings, traces.BACKEND, __version__, scrub=_scrub)
    metrics.build_info(__version__, settings.commit)
    # One span around what startup does: its queries are then one trace, not a trace each.
    with traces.span("startup"):
        redis = await init_redis()
        try:
            await redis.delete(EMBEDDINGS_PENDING_KEY)
        except Exception as exc:
            logger.warning("Redis unavailable at startup: %s", exc)
        await _recover_after_restart()
    watching = spawn(metrics.watch_event_loop())  # Phase 30: how late the event loop runs what is due
    yield
    watching.cancel()
    try:
        # The plans in flight first — they need Redis and the tool server, which are closed next.
        await finish_runs(settings.SHUTDOWN_GRACE_SECONDS)
        await close_redis()
        await close_session()
    finally:
        tracing.shutdown()  # the last traces, sent — for a few seconds at most, whatever else failed to close
        traces.shutdown()  # the last spans
        error_reporting.shutdown()  # and the last error reports


# ── App ────────────────────────────────────────────────────────────────────


app = FastAPI(
    title="AI Trip Planner",
    description="Multi-agent AI travel planner — flights, hotels, activities & itineraries.",
    version=__version__,
    lifespan=lifespan,
)

# Auth is a Bearer token (header, or ?token= for EventSource) — no cookies, so no
# credentialed CORS. Origins come from CORS_ORIGINS; production lists the real
# frontend origin(s) instead of a wildcard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.CORS_ORIGIN_REGEX or None,  # Phase 27: a host with an address per deployment
    allow_methods=["*"],
    allow_headers=["*"],
    # The PDF export names its file and says whether the map made it in (Phase 19). A page on
    # another origin cannot read either header unless it is listed here.
    expose_headers=["Content-Disposition", "X-Itinerary-Map"],
)


class NoStoreByDefault:
    """Every answer says `Cache-Control: no-store` unless its route said something else (Phase 27).

    In production the frontend's host proxies /api/* to this server, and a CDN
    in front of a proxy caches what it is allowed to. Nothing here may be: every
    answer is one traveller's, behind their token. Said by the origin, it does
    not depend on how any proxy is set up. (Plain ASGI, not BaseHTTPMiddleware:
    that one wraps the response's body, and the event stream must not be wrapped.)
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
            await send(message)

        await self.app(scope, receive, send_with_header)


app.add_middleware(NoStoreByDefault)


# ── Whose a request is (Phase 29) ──────────────────────────────────────────

_TRIP_IN_PATH = re.compile(r"^/trips/([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})(?:/|$)")


def _user_of(scope) -> str | None:
    """The user a request's login token names: from the Authorization header, or ?token= (an event stream's).

    None for no token, and for one that does not hold up — this only says whose the request claims
    to be, for the log; whether it may do what it asks is still the route's to decide.
    """
    token = None
    for name, value in scope.get("headers") or []:
        if name == b"authorization" and value[:7].lower() == b"bearer ":
            token = value[7:].decode("latin-1")
            break
    if token is None:
        token = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("token", [None])[0]
    if not token:
        return None
    try:
        return str(uuid.UUID(decode_access_token(token).get("sub", "")))
    except Exception:
        return None


class RequestContext:
    """Says which trip a request is about, and whose it is, before anything handles it.

    The trip from the address (/trips/<id>/…), the user from the login token. Both go into two
    context variables (src/telemetry/context.py), which is all it takes: every log line written
    while the request is handled carries them, every span is tagged with them, and so is everything
    the request starts and leaves behind — a planning run begins with a copy of this context.
    Plain ASGI, like the middleware above: the event stream must not be wrapped.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        about = _TRIP_IN_PATH.match(scope.get("path", ""))
        with telemetry_context.scope(trip_id=about.group(1).lower() if about else None, user_id=_user_of(scope)):
            await self.app(scope, receive, send)


app.add_middleware(RequestContext)


# ── Error envelope (Phase 17) ──────────────────────────────────────────────
# Every error body carries {"error": {"code", "message"}} for the frontend, next
# to FastAPI's own "detail" so existing clients keep working.


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={
            "detail": exc.detail,
            "error": {"code": HTTPStatus(exc.status_code).name, "message": str(exc.detail)},
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    message = "; ".join(f"{'.'.join(str(part) for part in err['loc'][1:])}: {err['msg']}" for err in exc.errors())
    return JSONResponse(
        status_code=422,
        content={
            "detail": jsonable_encoder(exc.errors()),
            "error": {"code": "VALIDATION_ERROR", "message": message},
        },
    )


@app.exception_handler(RedisConnectionError)
@app.exception_handler(RedisTimeoutError)
async def redis_is_away_handler(_: Request, exc: Exception) -> JSONResponse:
    """Phase 31 — Redis cannot be reached: said as what it is, something that passes.

    A request that needs Redis before it can begin (a plan's conversation is kept there) used to
    be answered 500, "something went wrong on our side": true, and of no use to someone deciding
    whether to try again. Nothing has been changed by a request that ends here.
    """
    logger.warning("A request could not be served: Redis cannot be reached (%s)", str(exc)[:160])
    message = "The planner cannot be reached for a moment. Nothing is lost — please try again shortly."
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "5"},
        content={"detail": message, "error": {"code": "SERVICE_UNAVAILABLE", "message": message}},
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """Phase 27 — a 500 in the same envelope as every other error, and saying nothing of what went wrong.

    The error itself goes where it can be read: this handler's caller raises it
    again once the answer is sent, into the log and into Sentry.
    """
    message = "Something went wrong on our side. It has been reported."
    return JSONResponse(
        status_code=500,
        content={"detail": message, "error": {"code": "INTERNAL_SERVER_ERROR", "message": message}},
    )


app.include_router(health_router)
app.include_router(auth_router)
app.include_router(trips_router)
app.include_router(users_router)  # Phase 16
app.include_router(admin_router)  # Phase 14

# Phase 29 — the two outermost layers, in this order: a request is counted (GET /metrics says how many,
# how many failed, how long) inside the span that is the root of its trace.
metrics.instrument_app(app, may_read_metrics)
traces.instrument_app(app)

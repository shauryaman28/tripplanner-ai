"""
Health-check endpoint.

GET /ping
→ { "postgres": "ok", "redis": "ok" }          both healthy
→ { "postgres": "error: <msg>", "redis": "ok" } partial failure

Per roadmap spec: errors go in body, never in HTTP status.

GET /ready   (Phase 27)
→ the same body; 200 when both are ok, 503 otherwise — a platform's health check.

GET /version (Phase 28)
→ { "version": "0.35.0", "commit": "3f2a…" | null } — what is running here.
"""

import logging

from fastapi import APIRouter, Response

from app import __version__
from app.core.config import settings
from app.db.redis import get_redis
from app.db.session import get_db_raw

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


def _problem(what: str, exc: Exception) -> str:
    """What went wrong, as anyone may read it: in production the kind of error, and the rest in the log.

    The full text of a connection error names hosts, ports and users — fine on a laptop, not on a
    public address that needs no login.
    """
    if settings.APP_ENV == "production":
        logger.warning("Health check: %s is not answering: %s", what, exc)
        return f"error: {type(exc).__name__}"
    return f"error: {exc}"


@router.get("/ping")
async def ping() -> dict[str, str]:
    results: dict[str, str] = {}

    try:
        await get_db_raw()
        results["postgres"] = "ok"
    except Exception as exc:
        results["postgres"] = _problem("Postgres", exc)

    try:
        r = await get_redis()
        if not await r.ping():
            raise RuntimeError("PING returned falsy")
        results["redis"] = "ok"
    except Exception as exc:
        results["redis"] = _problem("Redis", exc)

    return results


@router.get("/ready")
async def ready(response: Response) -> dict[str, str]:
    """Phase 27 — /ping's findings as a status: 200 when Postgres and Redis both answer, 503 when one does not.

    For a platform's health check, which reads the status and nothing else: a
    deployment whose database or Redis cannot be reached must not be the one
    that goes live. (/ping keeps answering 200 with the errors in its body.)
    """
    results = await ping()
    if any(state != "ok" for state in results.values()):
        response.status_code = 503
    return results


@router.get("/version")
async def version() -> dict[str, str | None]:
    """Phase 28 — what is running here: the app's version, and the commit it was built from.

    "The deploy succeeded" is what a platform says when it has started a
    container. "It is live" is this address answering with the commit that was
    merged — which is what the deploy pipeline waits for, and times
    (scripts/wait_for_release.py). `commit` is null where nobody said: a laptop.
    """
    return {"version": __version__, "commit": settings.commit or None}

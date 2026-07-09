"""
Health-check endpoint.

GET /ping
→ { "postgres": "ok", "redis": "ok" }      (Phase 1 done criterion)
→ 503 with detail on any failed check
"""

from fastapi import APIRouter, HTTPException

from app.db.redis import get_redis
from app.db.session import get_db_raw

router = APIRouter(tags=["health"])


@router.get("/ping")
async def ping() -> dict[str, str]:
    """
    Confirms live connectivity to both Postgres and Redis.
    Returns {"postgres": "ok", "redis": "ok"} on success.
    Raises 503 with a specific message on any failure.
    """
    results: dict[str, str] = {}

    # ── Postgres check ────────────────────────────────────────
    try:
        await get_db_raw()
        results["postgres"] = "ok"
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Postgres unreachable: {exc}",
        ) from exc

    # ── Redis check ───────────────────────────────────────────
    try:
        r = await get_redis()
        response = await r.ping()
        if not response:
            raise RuntimeError("PING returned falsy")
        results["redis"] = "ok"
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Redis unreachable: {exc}",
        ) from exc

    return results

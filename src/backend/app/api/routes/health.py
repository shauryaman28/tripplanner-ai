"""
Health-check endpoint.

GET /ping
→ { "postgres": "ok", "redis": "ok" }          both healthy
→ { "postgres": "error: <msg>", "redis": "ok" } partial failure

Per roadmap spec: errors go in body, never in HTTP status.
"""

from fastapi import APIRouter

from app.db.redis import get_redis
from app.db.session import get_db_raw

router = APIRouter(tags=["health"])


@router.get("/ping")
async def ping() -> dict[str, str]:
    results: dict[str, str] = {}

    try:
        await get_db_raw()
        results["postgres"] = "ok"
    except Exception as exc:
        results["postgres"] = f"error: {exc}"

    try:
        r = await get_redis()
        if not await r.ping():
            raise RuntimeError("PING returned falsy")
        results["redis"] = "ok"
    except Exception as exc:
        results["redis"] = f"error: {exc}"

    return results

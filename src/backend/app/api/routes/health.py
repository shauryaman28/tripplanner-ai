"""
Health-check endpoint.

GET /ping
→ { "postgres": "ok", "redis": "ok" }   both healthy
→ 503 with detail on any failed check
"""

from fastapi import APIRouter, HTTPException

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
        raise HTTPException(status_code=503, detail=f"Postgres unreachable: {exc}") from exc

    try:
        r = await get_redis()
        if not await r.ping():
            raise RuntimeError("PING returned falsy")
        results["redis"] = "ok"
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis unreachable: {exc}") from exc

    return results

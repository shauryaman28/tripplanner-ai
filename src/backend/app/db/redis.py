"""
Async Redis client.

Usage:
    from app.db.redis import get_redis, redis_client
    r = await get_redis()
    await r.ping()

The client is a module-level singleton initialised at startup (lifespan).
"""

import redis.asyncio as aioredis

from app.core.config import settings

# Initialised in app lifespan — None until startup completes
redis_client: aioredis.Redis | None = None


async def init_redis() -> aioredis.Redis:
    """Create and store the Redis client. Called once at app startup."""
    global redis_client
    redis_client = aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )
    return redis_client


async def close_redis() -> None:
    """Close Redis connection. Called at app shutdown."""
    global redis_client
    if redis_client:
        await redis_client.aclose()
        redis_client = None


async def get_redis() -> aioredis.Redis:
    """FastAPI dependency — returns the live Redis client."""
    if redis_client is None:
        raise RuntimeError("Redis not initialised. App startup may have failed.")
    return redis_client

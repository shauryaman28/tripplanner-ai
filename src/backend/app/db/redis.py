"""Async Redis client singleton."""

import redis.asyncio as aioredis

from app.core.config import settings

redis_client: aioredis.Redis | None = None


async def init_redis() -> aioredis.Redis:
    global redis_client
    redis_client = aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )
    return redis_client


async def close_redis() -> None:
    global redis_client
    if redis_client:
        await redis_client.aclose()
        redis_client = None


async def get_redis() -> aioredis.Redis:
    """FastAPI dependency — returns the live Redis client."""
    if redis_client is None:
        raise RuntimeError("Redis not initialised. App startup may have failed.")
    return redis_client

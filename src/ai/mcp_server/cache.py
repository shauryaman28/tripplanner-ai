"""Synchronous Redis caching for MCP tools.

MCP tool functions are synchronous, so we use a sync Redis client here.
If Redis is unavailable, caching is silently disabled — tools still work.

Every cache access logs [CACHE HIT] or [CACHE MISS] at INFO level so you
can verify caching in docker compose logs or a terminal session.
"""

import hashlib
import json
import logging
from typing import Any

from src.ai.mcp_server.config import mcp_settings

logger = logging.getLogger(__name__)

_client = None  # lazy singleton


def _get_client():
    """Return a live sync Redis client, or None if Redis is unreachable."""
    global _client
    if _client is not None:
        return _client
    try:
        import redis  # sync client

        c = redis.from_url(
            mcp_settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
        )
        c.ping()
        _client = c
        logger.info("MCP cache: Redis connected at %s", mcp_settings.REDIS_URL)
        return _client
    except Exception as exc:
        logger.warning("MCP cache: Redis unavailable (%s) — caching disabled.", exc)
        return None


def make_cache_key(prefix: str, params: dict) -> str:
    """Deterministic cache key: mcp:<prefix>:<md5 of sorted params>."""
    payload = json.dumps(params, sort_keys=True, default=str)
    digest = hashlib.md5(payload.encode()).hexdigest()
    return f"mcp:{prefix}:{digest}"


def get_cached_sync(key: str) -> Any | None:
    """Return cached value (already parsed from JSON), or None on miss/error."""
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
        if raw is not None:
            logger.info("[CACHE HIT]  %s", key)
            return json.loads(raw)
        logger.info("[CACHE MISS] %s", key)
        return None
    except Exception as exc:
        logger.warning("Cache get failed: %s", exc)
        return None


def set_cached_sync(key: str, value: Any, ttl: int = 900) -> None:
    """Store value as JSON with TTL (seconds). Silent no-op if Redis is down."""
    client = _get_client()
    if client is None:
        return
    try:
        client.setex(key, ttl, json.dumps(value, default=str))
    except Exception as exc:
        logger.warning("Cache set failed: %s", exc)


def reset_client() -> None:
    """Force a fresh Redis connection on next use. Useful in tests."""
    global _client
    _client = None

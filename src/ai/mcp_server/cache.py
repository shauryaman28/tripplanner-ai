"""Synchronous Redis caching for MCP tools.

MCP tool functions are synchronous, so we use a sync Redis client here.
If Redis is unavailable, caching is silently disabled — tools still work.

Every cache access logs [CACHE HIT] or [CACHE MISS] at INFO level so you
can verify caching in docker compose logs or a terminal session.

The tools run in worker threads (server.py), several at once: single_flight()
keeps two identical searches from both going to the provider (Phase 24).
"""

import hashlib
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from src.ai.mcp_server.config import mcp_settings

logger = logging.getLogger(__name__)

_client = None  # lazy singleton
_client_lock = threading.Lock()


def _get_client():
    """Return a live sync Redis client, or None if Redis is unreachable."""
    global _client
    if _client is not None:
        return _client
    # A plan's three searches arrive together, each in a thread of its own: one of them connects.
    with _client_lock:
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
        client.set(key, json.dumps(value, default=str), ex=ttl)
    except Exception as exc:
        logger.warning("Cache set failed: %s", exc)


# key → [its lock, how many callers hold or wait for it]. An entry lives only while someone does.
_in_flight: dict[str, list] = {}
_in_flight_guard = threading.Lock()


@contextmanager
def single_flight(key: str) -> Iterator[None]:
    """Hold a cache key while its answer is fetched: one search per key at a time.

        with single_flight(key):
            cached = get_cached_sync(key)       # the second caller finds what the first one stored
            if cached is None:
                ...ask the provider, set_cached_sync(key, ...)

    Without it, two identical searches that start together both miss the cache
    and both go to the provider — which is exactly what happens when a trip's
    caches are being warmed and its planning starts before the warming is done.
    Different keys never wait for each other.
    """
    with _in_flight_guard:
        entry = _in_flight.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _in_flight_guard:
            entry[1] -= 1
            if not entry[1]:
                del _in_flight[key]


def reset_client() -> None:
    """Force a fresh Redis connection on next use. Useful in tests."""
    global _client
    _client = None

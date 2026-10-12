"""Synchronous Redis caching for MCP tools.

MCP tool functions are synchronous, so we use a sync Redis client here.
If Redis is unavailable, caching is silently disabled — tools still work.

Every cache access logs [CACHE HIT] or [CACHE MISS] at INFO level so you
can verify caching in docker compose logs or a terminal session.

The tools run in worker threads (server.py), several at once: single_flight()
keeps two identical searches from both going to the provider (Phase 24).

Phase 29: every lookup is also tallied — hit, miss, or error when Redis could
not be asked — for the tool call it was made for (`counting`). The server sends
the tally to its caller with the answer; that is how a cache in this process
gets to be a metric in the other one.
"""

import hashlib
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from src.ai.mcp_server.config import mcp_settings
from src.telemetry import traces

logger = logging.getLogger(__name__)


class Tally:
    """What the cache did for one tool call: lookups by which cache, and by how each ended."""

    def __init__(self) -> None:
        self._counted: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def add(self, key: str, result: str) -> None:
        """`key` as make_cache_key writes it — mcp:<cache>[:version]:<digest>; `result`: hit, miss or error."""
        parts = key.split(":")
        cache = parts[1] if len(parts) >= 3 and parts[0] == "mcp" else "other"
        with self._lock:
            results = self._counted.setdefault(cache, {})
            results[result] = results.get(result, 0) + 1

    def counted(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {cache: dict(results) for cache, results in self._counted.items()}


# The tally of the tool call under way. A tool runs in a worker thread that starts with a copy of
# its caller's context, so the thread's lookups land in the tally the caller is holding.
_tally: ContextVar[Tally | None] = ContextVar("mcp_cache_tally", default=None)


@contextmanager
def counting(tally: Tally) -> Iterator[Tally]:
    """Tally every lookup made inside the block."""
    token = _tally.set(tally)
    try:
        yield tally
    finally:
        _tally.reset(token)


def _count(key: str, result: str) -> None:
    tally = _tally.get()
    if tally is not None:
        tally.add(key, result)


_client = None  # lazy singleton
_client_lock = threading.Lock()


def _where(redis_url: str) -> str:
    """A Redis as it may be logged: scheme, host and port — never the password in its URL."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(redis_url)
        return f"{parts.scheme}://{parts.hostname or '?'}{f':{parts.port}' if parts.port else ''}"
    except ValueError:
        return "an unreadable URL"


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
            from redis.backoff import ExponentialBackoff
            from redis.retry import Retry

            # Phase 27: a hosted Redis closes idle connections. A command that finds its connection
            # closed is sent again on a new one; one that has been idle for a while is pinged first.
            c = redis.from_url(
                mcp_settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_keepalive=True,
                health_check_interval=30,
                retry=Retry(ExponentialBackoff(cap=1.0, base=0.05), 3),
                retry_on_error=[redis.exceptions.ConnectionError],
            )
            c.ping()
            traces.instrument_redis(c)  # Phase 29: its commands are spans of the call they are made for
            _client = c
            # The host only: a hosted Redis's URL has its password in it.
            logger.info("MCP cache: Redis connected at %s", _where(mcp_settings.REDIS_URL))
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
        _count(key, "error")  # Redis is not there: nothing was found, and it was not a miss either
        return None
    try:
        raw = client.get(key)
        if raw is not None:
            logger.info("[CACHE HIT]  %s", key)
            _count(key, "hit")
            return json.loads(raw)
        logger.info("[CACHE MISS] %s", key)
        _count(key, "miss")
        return None
    except Exception as exc:
        logger.warning("Cache get failed: %s", exc)
        _count(key, "error")
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

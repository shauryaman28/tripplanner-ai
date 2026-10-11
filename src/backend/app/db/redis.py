"""Async Redis client singleton.

Phase 27 — a hosted Redis (Upstash) is reached over TCP with TLS, `rediss://`:
its REST API cannot subscribe, and the event stream is a subscription. The URL
is all it takes; what a managed service adds is that it closes connections —
idle ones after an hour, all of them when its own proxy is redeployed — and the
library's defaults (no retry, no health check) turn each of those into one
failed request. So:

- a command that finds its connection closed is sent again on a new one. Only
  that: a command that *timed out* may have arrived, and sending a PUBLISH twice
  shows the page the same piece of an itinerary twice;
- a subscription whose connection is closed connects and subscribes again by
  itself — the same retry, and the library's own doing. Seen on the stand-in:
  the connection under an open event stream killed in Redis, and what was
  published a second later still reached the page;
- a connection that has been idle for a while is pinged before it is used;
- and for the connection that dies without saying so (a proxy that drops it, no
  FIN), the operating system is asked to find out: TCP keep-alive probes after
  half a minute of silence, and three unanswered ones are a closed connection.
"""

import socket
from urllib.parse import urlsplit

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import settings

redis_client: aioredis.Redis | None = None

HEALTH_CHECK_S = 30  # ping a connection idle for this long before trusting it
RECONNECT_ATTEMPTS = 3
KEEPALIVE = {"idle": 30, "interval": 10, "probes": 3}  # silence → first probe; between probes; unanswered → closed


def keepalive_options() -> dict[int, int]:
    """TCP keep-alive as this platform's sockets name it (Linux: TCP_KEEPIDLE; macOS: TCP_KEEPALIVE)."""
    names = {
        "idle": ("TCP_KEEPIDLE", "TCP_KEEPALIVE"),
        "interval": ("TCP_KEEPINTVL",),
        "probes": ("TCP_KEEPCNT",),
    }
    options: dict[int, int] = {}
    for what, candidates in names.items():
        option = next((getattr(socket, name) for name in candidates if hasattr(socket, name)), None)
        if option is not None:
            options[option] = KEEPALIVE[what]
    return options


def connection_options() -> dict:
    """How every connection to Redis is made — the app's commands and its subscriptions alike."""
    return {
        "encoding": "utf-8",
        "decode_responses": True,
        "health_check_interval": HEALTH_CHECK_S,
        "socket_keepalive": True,
        "socket_keepalive_options": keepalive_options(),
        "socket_connect_timeout": 5,
        "retry": Retry(ExponentialBackoff(cap=1.0, base=0.05), RECONNECT_ATTEMPTS),
        "retry_on_error": [RedisConnectionError],
    }


def where(redis_url: str) -> str:
    """The Redis as it may be logged: scheme, host and port — never the password in the URL."""
    try:
        parts = urlsplit(redis_url)
        return f"{parts.scheme}://{parts.hostname or '?'}{f':{parts.port}' if parts.port else ''}"
    except ValueError:
        return "an unreadable URL"


async def init_redis() -> aioredis.Redis:
    global redis_client
    redis_client = aioredis.from_url(settings.REDIS_URL, **connection_options())
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

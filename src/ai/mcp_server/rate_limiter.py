"""How often each provider may be asked, and the queue a request waits in when that is used up (Phase 24).

Every provider counts requests and answers HTTP 429 past its limit. Three
searches run at once in a plan, a trip's caches are warmed while it is being
created, and several travellers plan at the same time: nothing but this stops
them from asking one provider too often. A request over the limit is not
refused here — it is given the next free moment and waits for it.

The limits are the providers' own, read from their response headers or their
documentation (docs/phase24_build_log.md has the probes):

    duffel        30 a minute    `ratelimit-limit: 30` on POST /air/offer_requests
    liteapi        5 a second    `x-ratelimit-limit: 5`
    opentripmap   10 a second    `x-ratelimit-limit: 10`
    openweather   60 a minute    the free plan's documented limit (no headers)
    nominatim      1 a second    the usage policy of the public server

The window slides: no stretch of `period` seconds, wherever it starts, holds
more than `calls` requests. That is never looser than a provider that counts
per calendar minute, as Duffel does. And it is a tenth longer than the
provider's (HEADROOM): a request is counted here when it leaves and there when
it arrives, so two requests sent a window apart can arrive a little closer.

The count lives in this process — the MCP server, which is the one place that
sends these requests. One backend process runs one MCP server; N backend
workers would each have a count of their own, and the limits would then have
to move to Redis.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_sleep = time.sleep  # a seam: tests record the waits instead of sitting through them

# How much longer our window is than the provider's — see the module docstring.
HEADROOM = 1.1

# The longest a request may be made to wait: one whole window of the slowest limit. Inside it a
# request always gets its turn; past it the queue is already a full window deep, and the caller
# is told to come back instead of being held for minutes.
MAX_WAIT_S = 60.0


@dataclass(frozen=True)
class Limit:
    calls: int
    period: float  # seconds


LIMITS: dict[str, Limit] = {
    "duffel": Limit(30, 60),
    "liteapi": Limit(5, 1),
    "opentripmap": Limit(10, 1),
    "openweather": Limit(60, 60),
    "nominatim": Limit(1, 1),
}


class RateLimited(Exception):
    """A provider could not be asked, or would not answer, because it is being asked too often."""

    provider: str


class QueueFull(RateLimited):
    """The wait for a provider's next free moment is longer than a request may be held."""

    def __init__(self, provider: str, wait: float):
        self.provider, self.wait = provider, wait
        super().__init__(
            f"{provider}: too many requests are already waiting (the next free moment is {wait:.0f} s away)"
        )


class StillLimited(RateLimited):
    """The provider answered HTTP 429 to every attempt (outbound.send)."""

    def __init__(self, provider: str, attempts: int):
        self.provider, self.attempts = provider, attempts
        super().__init__(f"{provider}: still HTTP 429 after {attempts} attempts")


class RateLimiter:
    """At most `calls` requests in any `period` seconds. Safe to share between threads.

    acquire() hands out moments in the order it is called: each caller is given
    the earliest one that keeps the limit and sleeps until then, so nobody waits
    behind a request that arrived later.
    """

    def __init__(
        self,
        name: str,
        calls: int,
        period: float,
        *,
        max_wait: float = MAX_WAIT_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.name, self.calls, self.period, self.max_wait = name, calls, period, max_wait
        self._clock, self._sleep = clock, sleep
        # When the last `calls` requests went, or will go: the oldest of them says when the next may.
        self._moments: deque[float] = deque(maxlen=calls)
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Wait for this request's turn. Returns the seconds waited; raises QueueFull past `max_wait`."""
        with self._lock:
            now = self._clock()
            moment = now
            if len(self._moments) == self.calls:
                moment = max(now, self._moments[0] + self.period)
            wait = moment - now
            if wait > self.max_wait:
                raise QueueFull(self.name, wait)
            self._moments.append(moment)

        if wait > 0:
            logger.info(
                "[RATE LIMIT] %s: limit of %d per %g s reached — waiting %.2f s",
                self.name,
                self.calls,
                self.period,
                wait,
            )
            self._sleep(wait)
        return wait


_limiters: dict[str, RateLimiter] = {}
_registry_lock = threading.Lock()


def limiter(provider: str) -> RateLimiter:
    """The one limiter of a provider (KeyError for a provider with no limit on record)."""
    with _registry_lock:
        if provider not in _limiters:
            limit = LIMITS[provider]
            _limiters[provider] = RateLimiter(
                provider, limit.calls, limit.period * HEADROOM, sleep=lambda seconds: _sleep(seconds)
            )
        return _limiters[provider]


def reset() -> None:
    """Forget every request counted so far. For tests: the count would otherwise carry from one to the next."""
    with _registry_lock:
        _limiters.clear()

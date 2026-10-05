"""The one way a request leaves for a provider: in its turn, and again after a wait when it is turned away (Phase 24).

    response = send("duffel", lambda: httpx.post(...))

send() does three things around the request it is given:

- waits for the provider's rate limit (rate_limiter.py), so the request is not
  one too many;
- raises for an error status, as `response.raise_for_status()` would;
- on an answer that a second try can change, waits and tries again — up to
  four attempts, each wait about twice the last, with jitter so that requests
  turned away together do not all come back together.

What a second try can change: HTTP 429 (asked too often — the limit here is
ours to keep, but a provider's count is shared with whoever else holds the key)
and 500 / 502 / 503 / 504, and a connection that was never made. What it
cannot: any other 4xx (a wrong key or a bad request stays wrong), and a read
timeout — the provider did get the request and has already been given its full
time, and three more of those would hold a traveller for minutes.

Every wait is logged, because a retry nobody can see looks like a slow search:

    [BACKOFF] duffel: HTTP 429 — waiting 1.30 s before attempt 2 of 4
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from src.ai.mcp_server.rate_limiter import StillLimited, limiter

logger = logging.getLogger(__name__)

ATTEMPTS = 4
MAX_BACKOFF_S = 30
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# 1 s, 2 s, 4 s … plus up to a second of jitter each, never more than 30 s.
_backoff = wait_exponential_jitter(initial=1, max=MAX_BACKOFF_S)
_sleep = time.sleep  # a seam: tests record the waits instead of sitting through them


def _worth_retrying(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRY_STATUSES
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))  # the request never reached the provider


def _asked_to_wait(response: httpx.Response) -> float:
    """Seconds the provider says to stay away: `Retry-After`, else Duffel's `ratelimit-reset`. 0 when it says nothing."""
    for header in ("retry-after", "ratelimit-reset"):
        value = (response.headers.get(header) or "").strip()
        if not value:
            continue
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            until = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            continue
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return max(0.0, (until - datetime.now(timezone.utc)).total_seconds())
    return 0.0


def _wait(retry_state: RetryCallState) -> float:
    """The backoff, or what the provider asked for when that is longer — capped either way."""
    wait = _backoff(retry_state)
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError):
        wait = max(wait, _asked_to_wait(exc.response))
    return min(wait, MAX_BACKOFF_S)


def _why(exc: BaseException | None) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__  # never str(exc): it can hold the request URL, API key included


def send(provider: str, request: Callable[[], httpx.Response]) -> httpx.Response:
    """Make `request` to `provider` and return its response. Raises httpx errors once the attempts are spent.

    `request` is called once per attempt, so it must build the request afresh
    each time. Two failures are rate_limiter.RateLimited instead: QueueFull when
    the provider's queue is too long to join, StillLimited when every attempt
    was answered 429.
    """

    def log_wait(retry_state: RetryCallState) -> None:
        logger.warning(
            "[BACKOFF] %s: %s — waiting %.2f s before attempt %d of %d",
            provider,
            _why(retry_state.outcome.exception()),
            retry_state.next_action.sleep,
            retry_state.attempt_number + 1,
            ATTEMPTS,
        )

    def attempt() -> httpx.Response:
        limiter(provider).acquire()
        response = request()
        response.raise_for_status()
        return response

    retrying = Retrying(
        wait=_wait,
        stop=stop_after_attempt(ATTEMPTS),
        retry=retry_if_exception(_worth_retrying),
        before_sleep=log_wait,
        sleep=lambda seconds: _sleep(seconds),
        reraise=True,
    )
    try:
        return retrying(attempt)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            raise StillLimited(provider, ATTEMPTS) from exc
        raise

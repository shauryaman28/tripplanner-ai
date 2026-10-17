"""How often one address may try to sign in, or make an account (Phase 35).

    POST /auth/login       LOGIN_ATTEMPTS_PER_MINUTE (5): the sixth within any 60 seconds is 429
    POST /auth/register    REGISTRATIONS_PER_MINUTE (5)

A sliding window, not a clock minute: a fixed minute lets ten through in two seconds across its
edge. Each address has a sorted set in Redis of the moments it tried; one Lua script trims what is
older than the window, counts, and adds the new attempt only if there is room — so two requests at
once cannot both take the last place, and an attempt that is turned away does not count. Turned
away, the client is told how long until the oldest attempt leaves the window (`Retry-After`).

Every attempt counts, the ones that succeed too: "attempts", as the roadmap says, and a password
guesser's successes are not the point.

**Which address.** `request.client.host`, as uvicorn worked it out — which is only as good as whom
uvicorn believes. Behind a proxy that address is the proxy's, and uvicorn takes the client from
`X-Forwarded-For` when the proxy is one it trusts. Until Phase 35 the image trusted everybody
(`--forwarded-allow-ips '*'`), and then uvicorn takes the *leftmost* entry — the one the client
wrote itself: a fresh made-up address with each guess, and no limit at all. The image now trusts
only private networks (where a platform's own proxy connects from) and walks the header from the
right, so the address used is the one the last trusted proxy saw. A deployment whose proxy connects
from somewhere else says so in `FORWARDED_ALLOW_IPS` (HOW_TO_RUN.md, Step 33).

**With Redis away** the attempt is let through, and the log says so: signing in needs only the
database, and a Redis outage should not lock everybody out. A password guesser gets the time Redis
is away, at the speed bcrypt allows (some tenths of a second a guess).
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)

WINDOW_S = 60.0

# KEYS[1] the address's set; ARGV: now, window, limit, a unique member. Returns {1, 0} when let in,
# {0, seconds to wait} when not.
_SCRIPT = """
local key, now, window, limit = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
if redis.call('ZCARD', key) >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  return {0, tostring(tonumber(oldest[2]) + window - now)}
end
redis.call('ZADD', key, now, ARGV[4])
redis.call('PEXPIRE', key, math.ceil(window * 1000))
return {1, '0'}
"""


def who(address: str) -> str:
    """An address as the log names it: eight characters of its hash. Phase 29 keeps addresses out of the log
    (whose a request was is `user_id`); this still shows one address turned away again and again — or every
    traveller turned away under the same one, which is a proxy nobody told uvicorn about."""
    return hashlib.sha256(address.encode()).hexdigest()[:8]


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    retry_after_s: int = 0  # when not allowed: whole seconds until there is room again


async def attempt(redis, action: str, address: str, limit: int, window_s: float = WINDOW_S) -> Verdict:
    """One attempt at `action` ("login", "register") from `address`. Never raises: Redis away lets it through."""
    if limit <= 0:  # no limit configured
        return Verdict(True)
    key = f"ratelimit:{action}:{address}"
    try:
        allowed, wait = await redis.eval(_SCRIPT, 1, key, time.time(), window_s, limit, uuid.uuid4().hex)
    except Exception as exc:  # Redis away, or no client at all: let it through, and say so
        logger.warning(
            "[RATE LIMIT] %s from client %s let through unlimited: Redis cannot be asked (%s)",
            action,
            who(address),
            exc,
        )
        return Verdict(True)
    if int(allowed):
        return Verdict(True)
    seconds = max(1, math.ceil(float(wait)))
    logger.warning(
        "[RATE LIMIT] %s from client %s refused: %d attempts in %.0f s — again in %d s",
        action,
        who(address),
        limit,
        window_s,
        seconds,
    )
    return Verdict(False, seconds)

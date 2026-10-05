"""What the MCP tools have cached in Redis, and when each entry expires — against the Phase 3 spec.

    python scripts/cache_ttls.py                 # the Redis in .env (REDIS_URL)
    python scripts/cache_ttls.py --url redis://localhost:6379/15

`redis-cli TTL` takes one key, not a pattern, so "TTL mcp:flights:*" answers -2
(no such key) whatever is cached. This reads every key of every family:

    cache         keys   expires in               spec
    flights          2   211–297 s                300 s          ok
    hotels           1   842 s                    900 s          ok
    attractions      1   21,544 s                 21,600 s       ok
    weather          1   3,546 s                  3,600 s        ok
    geocode          3   2,346,213–2,591,944 s    2,592,000 s    ok   (not in the Phase 3 spec)

A key is wrong when it never expires (TTL -1) or expires later than the spec
allows. Exit status 1 if any is; 0 otherwise — also when nothing is cached.

By hand, for one family:

    redis-cli --scan --pattern 'mcp:flights:*' | while read key; do echo "$(redis-cli ttl "$key")  $key"; done
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT)]

# The spec, in seconds: roadmap Phase 3 ("flights 5 min, hotels 15 min, attractions 6 hr, weather 1 hr").
# Written out here, not imported from the tools: this is what the tools' constants are checked against.
SPEC = {"flights": 300, "hotels": 900, "attractions": 21_600, "weather": 3_600}
# Kept by the tools as well, and not in the spec: where a place is. Places do not move.
BEYOND_SPEC = {"geocode": 2_592_000}


@dataclass(frozen=True)
class Family:
    name: str
    keys: int
    soonest: int  # seconds until the first key expires; -1 if one never does
    latest: int
    allowed: int | None  # None: a family nobody decided a TTL for

    @property
    def ok(self) -> bool:
        return self.allowed is not None and 0 < self.soonest and self.latest <= self.allowed


def report(client) -> list[Family]:
    """Every family of `mcp:<family>:…` keys in a Redis, in the spec's order. `client` is a sync redis client."""
    ttls: dict[str, list[int]] = {}
    for key in client.scan_iter("mcp:*"):
        key = key.decode() if isinstance(key, bytes) else key
        ttl = client.ttl(key)
        if ttl != -2:  # expired between the scan and the read
            ttls.setdefault(key.split(":")[1], []).append(ttl)

    allowed = {**SPEC, **BEYOND_SPEC}
    order = {name: place for place, name in enumerate(allowed)}
    return [
        Family(name, len(found), min(found), max(found), allowed.get(name))
        for name, found in sorted(ttls.items(), key=lambda item: (order.get(item[0], len(order)), item[0]))
    ]


def _line(family: Family) -> str:
    if family.soonest == -1:
        expires = "NEVER (no TTL)"
    elif family.soonest == family.latest:
        expires = f"{family.latest:,} s"
    else:
        expires = f"{family.soonest:,}–{family.latest:,} s"
    spec = f"{family.allowed:,} s" if family.allowed else "none"
    verdict = "ok" if family.ok else "WRONG"
    note = "   (not in the Phase 3 spec)" if family.name in BEYOND_SPEC else ""
    return f"{family.name:<13}{family.keys:>5}   {expires:<25}{spec:<15}{verdict}{note}"


def main() -> int:
    import redis

    from src.ai.mcp_server import tools
    from src.ai.mcp_server.config import mcp_settings

    parser = argparse.ArgumentParser(description="TTLs of the MCP tools' Redis cache, against the Phase 3 spec.")
    parser.add_argument("--url", default=mcp_settings.REDIS_URL, help="Redis to read (default: REDIS_URL)")
    url = parser.parse_args().url

    in_code = {
        "flights": tools.TTL_FLIGHTS,
        "hotels": tools.TTL_HOTELS,
        "attractions": tools.TTL_ATTRACTIONS,
        "weather": tools.TTL_WEATHER,
    }
    drift = {name: (in_code[name], seconds) for name, seconds in SPEC.items() if in_code[name] != seconds}
    for name, (code, spec) in drift.items():
        print(f"The code keeps {name} for {code:,} s; the spec says {spec:,} s.")

    try:
        families = report(redis.from_url(url, socket_connect_timeout=2))
    except redis.RedisError as exc:
        print(f"Redis is not reachable at {url}: {exc}")
        return 1

    if not families:
        print(
            "Nothing is cached (no mcp:* keys). Plan a trip, or create one and let its caches warm, then run this again."
        )
        return 1 if drift else 0
    print(f"{'cache':<13}{'keys':>5}   {'expires in':<25}{'spec':<15}")
    for family in families:
        print(_line(family))
    return 1 if drift or not all(family.ok for family in families) else 0


if __name__ == "__main__":
    sys.exit(main())

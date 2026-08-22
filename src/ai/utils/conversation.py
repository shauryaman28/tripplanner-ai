"""Redis-backed conversation history and planning state — Phase 7B."""

import json
import redis.asyncio as aioredis

_TTL = 86_400  # 24 h — same as JWT expiry


def _hist_key(trip_id: str) -> str:
    return f"trip:{trip_id}:conv_history"


def _state_key(trip_id: str) -> str:
    return f"trip:{trip_id}:planning_state"


async def get_history(r: aioredis.Redis, trip_id: str) -> list[dict]:
    raw = await r.get(_hist_key(trip_id))
    return json.loads(raw) if raw else []


async def append_history(
    r: aioredis.Redis, trip_id: str, role: str, content: str
) -> list[dict]:
    hist = await get_history(r, trip_id)
    hist.append({"role": role, "content": content})
    await r.setex(_hist_key(trip_id), _TTL, json.dumps(hist))
    return hist


async def get_trip_state(r: aioredis.Redis, trip_id: str) -> dict | None:
    raw = await r.get(_state_key(trip_id))
    return json.loads(raw) if raw else None


async def save_trip_state(r: aioredis.Redis, trip_id: str, state: dict) -> None:
    await r.setex(_state_key(trip_id), _TTL, json.dumps(state, default=str))

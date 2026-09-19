"""Redis-backed conversation history and planning state.

Phase 7B: initial implementation — {role, content} history entries.
Phase 15: history entries gain a `turn` field for multi-turn refinement.
          New helper: get_current_turn() derives the turn number from history.

All existing callers of append_history remain compatible: the `turn`
parameter defaults to 1, so Phase 7 / Phase 9 code needs no changes.
"""

import json

import redis.asyncio as aioredis

_TTL = 86_400  # 24 h — same as JWT expiry


# ── Key helpers ───────────────────────────────────────────────────────────


def _hist_key(trip_id: str) -> str:
    return f"trip:{trip_id}:conv_history"


def _state_key(trip_id: str) -> str:
    return f"trip:{trip_id}:planning_state"


# ── History ───────────────────────────────────────────────────────────────


async def get_history(r: aioredis.Redis, trip_id: str) -> list[dict]:
    raw = await r.get(_hist_key(trip_id))
    return json.loads(raw) if raw else []


async def append_history(
    r: aioredis.Redis,
    trip_id: str,
    role: str,
    content: str,
    turn: int = 1,
) -> list[dict]:
    """Append one message to the conversation history and return the full list.

    Each entry: {"role": str, "content": str, "turn": int}
    The `turn` parameter was added in Phase 15; defaults to 1 so all
    existing callers (Phase 7B, Phase 9) require no change.
    """
    hist = await get_history(r, trip_id)
    hist.append({"role": role, "content": content, "turn": turn})
    await r.setex(_hist_key(trip_id), _TTL, json.dumps(hist))
    return hist


async def get_current_turn(r: aioredis.Redis, trip_id: str) -> int:
    """Return the highest turn number seen in the conversation history.

    Returns 0 if there is no history yet (so the caller knows this is
    the very first interaction, turn 1 will be assigned).
    """
    hist = await get_history(r, trip_id)
    if not hist:
        return 0
    return max(entry.get("turn", 1) for entry in hist)


# ── Planning state ────────────────────────────────────────────────────────


async def get_trip_state(r: aioredis.Redis, trip_id: str) -> dict | None:
    raw = await r.get(_state_key(trip_id))
    return json.loads(raw) if raw else None


async def save_trip_state(r: aioredis.Redis, trip_id: str, state: dict) -> None:
    await r.setex(_state_key(trip_id), _TTL, json.dumps(state, default=str))

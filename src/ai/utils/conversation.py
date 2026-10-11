"""Redis-backed conversation history and planning state.

Phase 7B: initial implementation — {role, content} history entries.
Phase 15: history entries gain a `turn` field for multi-turn refinement.
          get_current_turn() derives the turn number from history.

Phase 20: the run in flight is noted while it runs, so a page loaded in the
          middle of it can be told what it is.

Keys (24 h TTL):
  trip:{id}:conv_history     list of {role, content, turn}
  trip:{id}:planning_state   the last SUCCESSFUL orchestrator state — what
                             POST /refine carries forward
  trip:{id}:current_run      {turn, refinement_type?, retry?, choice?} — set when a
                             run starts, deleted when it ends

Phase 27: the note of the run in flight is also its sign of life. It lives for
          RUN_TTL_S seconds and the run writes it again every RUN_BEAT_S, so a
          note that is there means a process is at work on that trip — this one
          or, during a deploy, the one being replaced. A note that is gone
          while the trip still says "planning" is a run that died.
"""

import json
import logging

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_TTL = 86_400  # 24 h — same as JWT expiry
RUN_BEAT_S = 15  # how often a run in flight writes its note again
RUN_TTL_S = 45  # how long a note lives without that: three beats, so one that fails changes nothing


# ── Key helpers ───────────────────────────────────────────────────────────


def _hist_key(trip_id: str) -> str:
    return f"trip:{trip_id}:conv_history"


def _state_key(trip_id: str) -> str:
    return f"trip:{trip_id}:planning_state"


def _run_key(trip_id: str) -> str:
    return f"trip:{trip_id}:current_run"


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
    """
    hist = await get_history(r, trip_id)
    hist.append({"role": role, "content": content, "turn": turn})
    await r.set(_hist_key(trip_id), json.dumps(hist), ex=_TTL)
    return hist


async def start_history(r: aioredis.Redis, trip_id: str, content: str) -> None:
    """Begin a new conversation: a fresh POST /plan is always turn 1."""
    await r.set(_hist_key(trip_id), json.dumps([{"role": "user", "content": content, "turn": 1}]), ex=_TTL)


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
    await r.set(_state_key(trip_id), json.dumps(state, default=str), ex=_TTL)


# ── The run in flight ─────────────────────────────────────────────────────


async def save_current_run(r: aioredis.Redis, trip_id: str, run: dict) -> None:
    """Note the run in flight. Called as it starts, and again every RUN_BEAT_S while it lasts."""
    await r.set(_run_key(trip_id), json.dumps(run, default=str), ex=RUN_TTL_S)


async def run_in_flight(r: aioredis.Redis, trip_id: str) -> bool:
    """Is some process at work on this trip right now? Its note says so for as long as it is kept up."""
    return bool(await r.exists(_run_key(trip_id)))


async def clear_current_run(r: aioredis.Redis, trip_id: str) -> None:
    await r.delete(_run_key(trip_id))


async def get_current_run(r: aioredis.Redis | None, trip_id: str) -> dict | None:
    """What the run in flight is, or None. It only describes the run — a missing or unreadable note is not an error."""
    if r is None:
        return None
    try:
        run = json.loads(await r.get(_run_key(trip_id)) or "null")
    except Exception:
        logger.debug("Could not read the current run of trip %s", trip_id, exc_info=True)
        return None
    return run if isinstance(run, dict) else None

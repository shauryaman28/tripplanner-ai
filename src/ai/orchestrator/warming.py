"""Cache warming — a trip's opening searches, made while the trip is being created (Phase 24).

A plan opens with a flight search; hotels and attractions follow. Each is a
request to a provider that takes seconds, and each answer is cached
(mcp_server/tools.py). A traveller creates a trip, lands on its page, and takes
a moment to say what they want — long enough to have made those searches
already. POST /trips starts this as a background task; when planning asks, the
answer is in the cache — or still on its way, in which case planning waits for
that one request instead of sending a second (cache.single_flight).

What is warmed is what planning will ask for, built by the same functions
(orchestrator.*_search_input, the agents' *_tool_params). A warmed entry under
any other key would be a request to a rate-limited provider, wasted.

    flights       origin: the saved home city, else Delhi — what planning assumes too
    hotels        possible because the cache key leaves the nightly budget out
    weather       the roadmap's pair with flights; nothing reads it before Phase 38
    attractions   only when the trip names its interests. With none, planning asks the
                  traveller for them or reads them from the request: the search is not known yet.
                  A group (Phase 25) is searched member by member, and warmed the same way

A trip whose destination is a sentence ("a relaxed week somewhere in Kerala")
is not warmed at all — where it goes is known only once planning has read it.
A plan whose request names another origin ("from Mumbai") searches other
flights, and simply misses.

Nothing here can fail a request: the trip is saved before warming starts, and
every error ends in a log line.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from src.ai import group
from src.ai.agents.activities_agent import attraction_searches
from src.ai.agents.flight_agent import flight_tool_params
from src.ai.agents.hotel_agent import hotel_tool_params
from src.ai.mcp_client.client import call_tool
from src.ai.orchestrator.orchestrator import (
    _is_free_text,
    activities_search_input,
    flight_search_input,
    hotel_search_input,
)
from src.ai.utils.preferences import build_preference_updates, load_preferences, preferences_to_dict
from src.ai.utils.tasks import spawn

logger = logging.getLogger(__name__)

WARMED = "warmed"


async def _warm(cache: str, tool: str, params: dict, what: str) -> tuple[str, str]:
    """One search. Returns (cache, "warmed" or the tool's error code) and logs which."""
    started = time.perf_counter()
    try:
        result = await call_tool(tool, params)
    except Exception as exc:  # call_tool itself never raises; this is for whatever stands in for it
        logger.warning("[CACHE WARM] %s: %s — the search failed: %s", cache, what, exc)
        return cache, "MCP_CLIENT_ERROR"
    took = time.perf_counter() - started

    if hasattr(result, "code"):
        logger.info("[CACHE WARM] %s: %s — answered %s in %.1f s: %s", cache, what, result.code, took, result.error)
        return cache, result.code
    logger.info("[CACHE WARM] %s: %s — %d result(s) ready in %.1f s", cache, what, len(result), took)
    return cache, WARMED


async def warm_trip_caches(trip: dict, preferences: dict | None = None) -> dict[str, str]:
    """Make a trip's opening searches, all at once. Returns {cache: "warmed" or an error code}.

    `trip` is the orchestrator's initial state for the trip (destination, dates,
    budget, group size, interests); `preferences` the traveller's saved ones, as
    planning loads them. Empty when nothing could be warmed. Never raises.
    """
    state = dict(trip)
    destination = (state.get("destination") or "").strip()
    if not destination or _is_free_text(destination):
        logger.info("[CACHE WARM] skipped: %r is a request, not a place — planning reads it first", destination)
        return {}
    if preferences is not None:
        state.update(build_preference_updates(state, preferences))

    start, end = state.get("start_date"), state.get("end_date")
    flights = flight_tool_params(flight_search_input(state))
    hotels = hotel_tool_params(hotel_search_input(state))
    weather = {"destination": destination, "date_range": f"{start} to {end}"}
    searches = [
        ("flights", "search_flights", flights, f"{flights['origin']} → {destination} on {start}, back {end}"),
        ("hotels", "search_hotels", hotels, f"{destination}, {start} to {end}, {hotels['guests']} guest(s)"),
        ("weather", "get_weather", weather, f"{destination}, {start} to {end}"),
    ]
    if trip.get("interests") or group.is_group(trip.get("group_members")):
        for attractions in attraction_searches(activities_search_input(state)):  # one — or one per member
            about = f"{destination}: {', '.join(map(str, attractions['interests']))}"
            searches.append(("attractions", "get_attractions", attractions, about))

    outcomes: dict[str, str] = {}
    for cache, outcome in await asyncio.gather(*(_warm(*search) for search in searches)):
        # a group's attractions are several searches: "warmed" only if every one of them was
        if outcomes.get(cache, WARMED) == WARMED:
            outcomes[cache] = outcome
    return outcomes


async def _saved_preferences(user_id: uuid.UUID) -> dict | None:
    """The traveller's preferences as planning will load them (orchestrator.apply_preferences_node); None if none."""
    try:
        from app.db.session import AsyncSessionLocal
    except ImportError:
        from src.backend.app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        prefs = await load_preferences(db, user_id)
    return preferences_to_dict(prefs) if prefs is not None else None


async def _warm_for(trip: dict, user_id: uuid.UUID) -> None:
    """The background task. A request has already been answered when this runs: it logs, it never raises."""
    try:
        await warm_trip_caches(trip, await _saved_preferences(user_id))
    except Exception:
        logger.exception("[CACHE WARM] failed — planning will make its own searches")


def start_cache_warming(trip: dict, user_id: uuid.UUID) -> asyncio.Task:
    """Start warming a trip's caches in the background; returns at once."""
    return spawn(_warm_for(trip, user_id))

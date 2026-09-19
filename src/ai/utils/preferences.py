"""Preference loading, injection and additive merging — Phase 16.

Everything here is either a pure function or a single-row DB read, so the
orchestrator nodes and the PreferenceExtractor stay thin.

Injection model
───────────────
The sub-agents' search nodes are structured pass-throughs (no system
prompt), so "injecting preferences into a sub-agent" means adjusting the
structured input that agent receives. That is done ONCE, in state, right
after intent parsing (see orchestrator.apply_preferences_node):

    home_city             → state["origin"]   (only if no origin was given)
    dietary_restrictions  → state["interests"] (appended, de-duplicated)
    travel_style          → default interest categories, only when the trip
                            has no explicit interests at all
    preferred_airlines    → read back by the flight-input builders via
                            preferred_airlines_from(state)
    everything            → state["preferences"] (context for ItineraryBuilder)
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

try:
    from app.core.preferences import MAX_LIST_ITEMS, merge_unique
    from app.models.trip import Trip
    from app.models.user_preferences import UserPreferences
except ImportError:
    from src.backend.app.core.preferences import MAX_LIST_ITEMS, merge_unique
    from src.backend.app.models.trip import Trip
    from src.backend.app.models.user_preferences import UserPreferences

logger = logging.getLogger(__name__)

# Default activity categories for a style when the trip states no interests.
# Values must be interests the get_attractions tool understands.
#   budget → "nature": free / low-cost outdoor attractions
#   luxury → "wellness": spas and premium experiences
STYLE_DEFAULT_INTERESTS: dict[str, list[str]] = {
    "budget": ["nature"],
    "luxury": ["wellness"],
}


# ── Loading ────────────────────────────────────────────────────────────────


async def get_trip_user_id(db: Any, trip_id: uuid.UUID) -> uuid.UUID | None:
    """Return the owning user's id for a trip, or None if it cannot be resolved."""
    trip = await db.get(Trip, trip_id)
    user_id = getattr(trip, "user_id", None)
    return user_id if isinstance(user_id, uuid.UUID) else None


async def load_preferences(db: Any, user_id: uuid.UUID) -> UserPreferences | None:
    """Return the user's preference row, or None. Never raises."""
    try:
        prefs = await db.get(UserPreferences, user_id)
    except Exception:
        logger.exception("Could not load preferences for user %s", user_id)
        return None
    return prefs if isinstance(prefs, UserPreferences) else None


def preferences_to_dict(prefs: UserPreferences | None) -> dict[str, Any]:
    """JSON-safe snapshot of the four preference fields (empty defaults for None)."""
    if prefs is None:
        return {"dietary_restrictions": [], "preferred_airlines": [], "travel_style": None, "home_city": None}
    return {
        "dietary_restrictions": list(prefs.dietary_restrictions or []),
        "preferred_airlines": list(prefs.preferred_airlines or []),
        "travel_style": prefs.travel_style,
        "home_city": prefs.home_city,
    }


# ── Injection (pure) ───────────────────────────────────────────────────────


def resolve_origin_code(home_city: str | None) -> str | None:
    """Map a home city (or an IATA code) to the airport code FlightAgent needs.

    The MCP server's city→IATA table is the single source of truth. It is
    imported lazily so that importing this module does not pull the Amadeus
    SDK. The city table is tried first because some city names are also
    valid 3-letter strings ("Goa" must resolve to GOI, not "GOA").
    """
    if not home_city or not home_city.strip():
        return None
    city = home_city.strip()
    try:
        from src.ai.mcp_server.tools import _city_to_iata

        code = _city_to_iata(city)
    except ImportError:
        code = None
    if code:
        return code
    return city.upper() if len(city) == 3 and city.isalpha() else None


def build_preference_updates(state: Mapping[str, Any], prefs: Mapping[str, Any]) -> dict[str, Any]:
    """Return the state fields to overwrite so *prefs* take effect.

    Pure and idempotent: applying the result twice changes nothing, which
    matters because /clarify and refine re-enter the graph with state that
    was already augmented on a previous pass.
    """
    updates: dict[str, Any] = {"preferences": dict(prefs)}

    if not state.get("origin"):
        origin = resolve_origin_code(prefs.get("home_city"))
        if origin:
            updates["origin"] = origin

    explicit = list(state.get("interests") or [])
    extra: list[str] = []
    if not explicit:
        extra.extend(STYLE_DEFAULT_INTERESTS.get(prefs.get("travel_style") or "", []))
    extra.extend(prefs.get("dietary_restrictions") or [])
    merged = merge_unique(explicit, extra)
    if merged != explicit:
        updates["interests"] = merged

    return updates


def preferred_airlines_from(state: Mapping[str, Any]) -> list[str]:
    """IATA carrier codes to forward to FlightAgent ([] when none are saved)."""
    return list((state.get("preferences") or {}).get("preferred_airlines") or [])


# ── Additive merge (pure) ──────────────────────────────────────────────────


def merge_extracted(existing: Mapping[str, Any], extracted: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the fields that change when *extracted* is folded into *existing*.

    Additive by construction — the extractor can never remove or replace
    something the user (or an earlier trip) already set:
      • list fields   → union, existing items first
      • scalar fields → filled only when currently unset
    An empty result means "nothing to write".
    """
    changes: dict[str, Any] = {}

    for key in ("dietary_restrictions", "preferred_airlines"):
        current = list(existing.get(key) or [])
        merged = merge_unique(current, extracted.get(key) or [], limit=MAX_LIST_ITEMS)
        if merged != current:
            changes[key] = merged

    for key in ("travel_style", "home_city"):
        if not existing.get(key) and extracted.get(key):
            changes[key] = extracted[key]

    return changes

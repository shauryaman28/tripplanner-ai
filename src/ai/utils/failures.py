"""A failed search, as the traveller is told about it (Phase 20).

The tools word their errors for whoever runs the server: "Duffel API not
configured. Set DUFFEL_ACCESS_TOKEN in .env.", "Pass an IATA code or add the
city to _CITY_IATA in tools.py." The trip page shows why each search failed and
offers to run it again, so it gets the reason in the traveller's words — and
whether running it again can help. A provider that was down may answer now; a
destination no airport is known for will not have one on the second try, and
a Retry button there would only fail again.
"""

from __future__ import annotations

# Said instead of the tool's words. Any other code keeps the tool's message, which is written for
# a traveller already ("No hotels within ₹4,000/night in Goa.", "Could not find a place called X.").
_WORDS = {
    "API_NOT_CONFIGURED": "This search is not set up on this server.",
    "UNKNOWN_DESTINATION": (
        "No airport is known by this name. Write the destination in English, or as its airport code, to include flights."
    ),
    "DUFFEL_ERROR": "The flight provider did not answer.",
    "LITEAPI_ERROR": "The hotel provider did not answer.",
    "OTM_ERROR": "The attractions provider did not answer.",
    # Phase 24: asked too often, and still turned away after the waits and retries (mcp_server/outbound.py)
    "RATE_LIMITED": "This search is busy right now. Try again in a minute.",
    "UNKNOWN_ERROR": "The search could not be completed.",
    "AGENT_EXCEPTION": "The search could not be completed.",
}

# Failures that come back the same on a second try: nothing about the trip has changed. Any other
# failure — a provider that errored, timed out or was not reachable — may not.
_LASTING = frozenset(
    {
        "API_NOT_CONFIGURED",
        "UNKNOWN_DESTINATION",
        "NOT_FOUND",
        "NO_RESULTS",
        "OUTSIDE_COVERAGE",
        "PAST_DATE",
        "INVALID_DATES",
        "INVALID_DATE_RANGE",
        "BUDGET_TOO_LOW",
        "LIMIT_EXCEEDED",
        "MISSING_DESTINATION",
        "MISSING_INPUT",
        "INVALID_INPUT",
    }
)


def failure_words(error: dict | None) -> str:
    """Why a search failed, for the traveller. `error` is the agent's {"code", "error"}."""
    error = error or {}
    return _WORDS.get(error.get("code") or "") or error.get("error") or "The search could not be completed."


def can_retry(error: dict | None) -> bool:
    """Whether running the search again could find something this time."""
    return (error or {}).get("code") not in _LASTING

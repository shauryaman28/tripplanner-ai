"""
Phase 8 Dev B — ActivitiesAgent: intent parsing, routing, and attraction search.
Phase 15: run() gains a `turn` parameter forwarded to log_agent_run.
Phase 25: a group is searched member by member. Each member who said what they
          enjoy gets a search of their own, at the same time as the others; the
          finds are merged, each place says who it suits and scores as the share
          of the group that is, and the places the plan is made from are taken
          in turns so that nobody's interests crowd out anybody else's
          (src/ai/group.py).
"""

from __future__ import annotations

import asyncio
import uuid

from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypedDict

from src.ai import group
from src.ai.llm import ask, parse_json_object
from src.ai.mcp_client.client import call_tool
from src.ai.utils.run_logger import log_agent_run, timed_run

# ── State ─────────────────────────────────────────────────────────────────


class ActivitiesState(TypedDict, total=False):
    destination: str
    interests: list[str]
    limit: int
    attractions: list[dict]
    error: dict | None
    raw_input: str | None
    clarification_question: str | None
    conversation_history: list[dict]
    # Phase 25 — a group: who travels and what each enjoys, and how long the trip is
    group_members: list[dict]
    days: int
    group_searches: list[dict]  # what each member's own search found


# ── Prompt ────────────────────────────────────────────────────────────────

_INTENT_PROMPT = """You are a travel assistant. Extract activity preferences from the user message.

Return ONLY a JSON object with these exact keys (use null for missing values):
{{
  "destination": "<city name or null>",
  "interests": ["list", "of", "interest", "keywords"] or null,
  "limit": <integer 1-10 or null>
}}

Rules:
- Extract all mentioned activity types as short English keywords (e.g. "history", "food", "adventure", "beach", "culture", "shopping", "nightlife", "nature")
- Translate non-English interest words to their English equivalents where possible (e.g. "खाना" → "food", "इतिहास" → "history")
- If no specific interests are mentioned, use null
- If no destination is mentioned, use null
- Include all distinct interests mentioned, even if there are many
- Return ONLY the JSON, no explanation, no markdown fences

User message: {message}"""


# ── Nodes ─────────────────────────────────────────────────────────────────


async def intent_parsing_node(state: ActivitiesState) -> ActivitiesState:
    raw = state.get("raw_input")
    if not raw:
        return state

    prompt = _INTENT_PROMPT.format(message=raw)
    parsed = parse_json_object(await ask(prompt))

    updates: ActivitiesState = {}

    if parsed.get("destination") and not state.get("destination"):
        updates["destination"] = parsed["destination"]

    parsed_interests = parsed.get("interests")
    if parsed_interests and not state.get("interests"):
        updates["interests"] = parsed_interests

    if parsed.get("limit") and not state.get("limit"):
        updates["limit"] = parsed["limit"]

    return {**state, **updates}


def router(state: ActivitiesState) -> str:
    if not state.get("destination"):
        return "clarify"
    interests = state.get("interests")
    if not interests and not group.is_group(state.get("group_members")):
        return "clarify"
    return "search"


async def clarify_node(state: ActivitiesState) -> ActivitiesState:
    if not state.get("destination"):
        question = "Which city would you like to explore?"
    else:
        question = "What kinds of activities do you enjoy? (e.g. history, food, adventure, beach)"

    return {**state, "clarification_question": question, "attractions": [], "error": None}


def attraction_tool_params(state: ActivitiesState) -> dict:
    """What `get_attractions` is called with. Cache warming makes the same call (src/ai/orchestrator/warming.py)."""
    return {
        "destination": state["destination"],
        "interests": state.get("interests", []),
        "limit": state.get("limit", 5),
    }


def attraction_searches(state: ActivitiesState) -> list[dict]:
    """Every `get_attractions` call a search makes: one — or, for a group, one for each member with interests.

    Cache warming makes the same calls (src/ai/orchestrator/warming.py).
    """
    if not group.is_group(state.get("group_members")):
        return [attraction_tool_params(state)]
    return [
        {"destination": state["destination"], "interests": member["interests"], "limit": group.MEMBER_SEARCH_LIMIT}
        for member in group.profiles(state["group_members"])
    ]


async def get_attractions_node(state: ActivitiesState) -> ActivitiesState:
    if group.is_group(state.get("group_members")):
        return await _search_for_group(state)

    result = await call_tool("get_attractions", attraction_tool_params(state))

    if hasattr(result, "code"):
        return {**state, "error": result.model_dump(), "attractions": []}

    return {**state, "attractions": result, "error": None}


async def _search_for_group(state: ActivitiesState) -> ActivitiesState:
    """One search per member, all at once; then one list with everyone's share in it.

    A member whose interests find nothing is not a failed search — the others
    still have theirs, and the plan says whose came up empty. A search that
    fails for any other reason fails the whole search: half a group's places
    would be a plan that looks balanced and is not, and the page offers a retry.
    """
    members = group.profiles(state["group_members"])
    results = await asyncio.gather(*(call_tool("get_attractions", params) for params in attraction_searches(state)))

    found: list[tuple[dict, list[dict]]] = []
    searches: list[dict] = []
    for member, result in zip(members, results):
        if hasattr(result, "code") and result.code != "NO_RESULTS":
            return {**state, "error": result.model_dump(), "attractions": [], "group_searches": []}
        attractions = [] if hasattr(result, "code") else list(result)
        found.append((member, attractions))
        searches.append(
            {
                "name": member["name"],
                "interests": member["interests"],
                "found": len(attractions),
                "nothing_for": group.nothing_for(member, attractions),
            }
        )

    pool = group.merge_for_group(found, group.clean_members(state["group_members"]))
    if not pool:
        error = {
            "error": f"No attractions found for anyone in the group in {state['destination']}.",
            "code": "NO_RESULTS",
        }
        return {**state, "error": error, "attractions": [], "group_searches": searches}

    count = group.attraction_count(members, state.get("days") or 1)
    picked = group.pick_for_group(pool, members, count)
    return {**state, "attractions": picked, "group_searches": searches, "error": None}


# ── Graph ─────────────────────────────────────────────────────────────────


def build_activities_agent_graph():
    graph = StateGraph(ActivitiesState)

    graph.add_node("intent_parsing", intent_parsing_node)
    graph.add_node("get_attractions", get_attractions_node)
    graph.add_node("clarify", clarify_node)

    graph.set_entry_point("intent_parsing")

    graph.add_conditional_edges(
        "intent_parsing",
        router,
        {"search": "get_attractions", "clarify": "clarify"},
    )

    graph.add_edge("get_attractions", END)
    graph.add_edge("clarify", END)

    return graph.compile()


# ── Agent wrapper ─────────────────────────────────────────────────────────


class ActivitiesAgent:
    def __init__(self):
        self._graph = build_activities_agent_graph()

    async def run(
        self,
        input_state: dict,
        db: AsyncSession | None = None,
        trip_id: uuid.UUID | None = None,
        turn: int = 1,
    ) -> dict:
        """Phase 15: `turn` parameter forwarded to log_agent_run. Defaults to 1."""
        async with timed_run() as timer:
            result = await self._graph.ainvoke(input_state)

        if db is not None and trip_id is not None:
            await log_agent_run(
                db=db,
                trip_id=trip_id,
                agent_name="activities_agent",
                input=input_state,
                output={
                    "attractions": result.get("attractions", []),
                    "error": result.get("error"),
                    **({"group_searches": result["group_searches"]} if result.get("group_searches") else {}),
                },
                duration_ms=timer.duration_ms,
                status="failed" if result.get("error") is not None else "completed",
                turn=turn,
            )

        return result

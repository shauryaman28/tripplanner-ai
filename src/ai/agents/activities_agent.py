"""
Phase 8 Dev B — ActivitiesAgent: intent parsing, routing, and attraction search.

Three-node graph (same pattern as FlightAgent / HotelAgent):
  - intent_parsing_node  : LLM extracts destination and interests from free-form text.
                           Includes a translation hint for non-English inputs (e.g. "खाना").
  - router               : deterministic Python — checks destination AND interests.
                           Both are required; an empty interests list → clarify.
  - get_attractions_node : calls get_attractions via MCP client. Pure function.
  - clarify_node         : returns a clarifying question, no tool call.

Required fields: destination, at least one interest in interests list.
Optional:        limit (defaults to 5).

Non-English interest handling:
  The prompt instructs the LLM to translate non-English interests where possible
  (e.g. Hindi "खाना" → "food"). However, the get_attractions_node does NOT validate
  or filter — it passes whatever interests are in state directly to the MCP tool,
  which embeds them into the Google Maps query string. Results may degrade for
  untranslated terms, but the system never crashes. See prompts/activities_agent_v2.md.
"""

from __future__ import annotations

import uuid

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypedDict

from src.ai.mcp_client.client import call_tool
from src.ai.utils.run_logger import log_agent_run, timed_run


# ── State ─────────────────────────────────────────────────────────────────


class ActivitiesState(TypedDict, total=False):
    # search fields
    destination: str
    interests: list[str]  # e.g. ["history", "street food", "beach"]
    limit: int            # max attractions to return (default 5)
    # results
    attractions: list[dict]
    error: dict | None
    # free-form input and clarification (Phase 8)
    raw_input: str | None
    clarification_question: str | None
    conversation_history: list[dict]


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
    """Extract destination and interests from free-form text via Gemini Flash.

    If raw_input is absent (caller already provided structured fields),
    passes state through unchanged — backward-compatible with structured callers.
    """
    raw = state.get("raw_input")
    if not raw:
        return state

    import json

    llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)
    prompt = _INTENT_PROMPT.format(message=raw)

    response = await llm.ainvoke(prompt)
    text = response.content.strip()

    # strip markdown fences if the model wraps its output
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.removeprefix("json")
    text = text.strip()

    try:
        parsed = json.loads(text)
    except Exception:
        return state

    updates: ActivitiesState = {}

    if parsed.get("destination") and not state.get("destination"):
        updates["destination"] = parsed["destination"]

    # Only fill interests if they are genuinely absent — don't overwrite
    # a caller-supplied list with what the LLM extracts from raw_input
    parsed_interests = parsed.get("interests")
    if parsed_interests and not state.get("interests"):
        updates["interests"] = parsed_interests

    if parsed.get("limit") and not state.get("limit"):
        updates["limit"] = parsed["limit"]

    return {**state, **updates}


def router(state: ActivitiesState) -> str:
    """Deterministic routing — no LLM, no network calls.

    Both destination AND at least one interest are required.
    An empty interests list is treated the same as None — it would produce
    a meaningless MCP query ("top  attractions in <city>").
    """
    if not state.get("destination"):
        return "clarify"
    interests = state.get("interests")
    if not interests:
        return "clarify"
    return "search"


async def clarify_node(state: ActivitiesState) -> ActivitiesState:
    """Return a plain-English clarifying question.

    No LLM, no tool call — deterministic and unit-testable.
    Prioritises destination first, then asks about interests.
    """
    if not state.get("destination"):
        question = "Which city would you like to explore?"
    else:
        question = "What kinds of activities do you enjoy? (e.g. history, food, adventure, beach)"

    return {**state, "clarification_question": question, "attractions": [], "error": None}


async def get_attractions_node(state: ActivitiesState) -> ActivitiesState:
    """Call get_attractions via the MCP client; populate attractions or error.

    Non-English interests are forwarded as-is — the MCP tool embeds them
    in the Google Maps query string and handles translation implicitly.
    Pure function — no side effects, no logging here.
    """
    params = {
        "destination": state["destination"],
        "interests": state.get("interests", []),
        "limit": state.get("limit", 5),
    }

    result = await call_tool("get_attractions", params)

    if hasattr(result, "code"):  # ToolError instance
        return {**state, "error": result.model_dump(), "attractions": []}

    return {**state, "attractions": result, "error": None}


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
    """Thin wrapper so callers don't need to touch LangGraph directly."""

    def __init__(self):
        self._graph = build_activities_agent_graph()

    async def run(
        self,
        input_state: dict,
        db: AsyncSession | None = None,
        trip_id: uuid.UUID | None = None,
    ) -> dict:
        async with timed_run() as timer:
            result = await self._graph.ainvoke(input_state)

        if db is not None and trip_id is not None:
            await log_agent_run(
                db=db,
                trip_id=trip_id,
                agent_name="activities_agent",
                input=input_state,
                output={"attractions": result.get("attractions", []), "error": result.get("error")},
                duration_ms=timer.duration_ms,
                status="failed" if result.get("error") is not None else "completed",
            )

        return result

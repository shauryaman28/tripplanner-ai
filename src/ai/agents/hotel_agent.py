"""
Phase 8 Dev A — HotelAgent: intent parsing, routing, and hotel search.
Phase 15: run() gains a `turn` parameter forwarded to log_agent_run.
"""

from __future__ import annotations

import uuid

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypedDict

from src.ai.mcp_client.client import call_tool
from src.ai.utils.run_logger import log_agent_run, timed_run

# ── State ──────────────────────────────────────────────────────────────────


class HotelState(TypedDict, total=False):
    destination: str
    check_in: str
    check_out: str
    budget_per_night: float
    guests: int
    hotels: list[dict]
    error: dict | None
    raw_input: str | None
    clarification_question: str | None
    conversation_history: list[dict]


# ── Prompt ────────────────────────────────────────────────────────────────

_INTENT_PROMPT = """You are a travel assistant. Extract hotel booking fields from the user message.

Return ONLY a JSON object with these exact keys (use null for missing values):
{{
  "destination": "<city name or null>",
  "check_in": "<YYYY-MM-DD or null>",
  "check_out": "<YYYY-MM-DD or null>",
  "budget_per_night": <number in INR or null>,
  "guests": <integer or null>
}}

Rules:
- Convert relative dates using today as reference: {today}
- check_in is the arrival/start date; check_out is the departure/end date
- If the user gives a total hotel budget and number of nights can be inferred, divide to get per-night figure
- If a field is not mentioned at all, use null
- Return ONLY the JSON, no explanation, no markdown fences

User message: {message}"""


# ── Nodes ─────────────────────────────────────────────────────────────────


async def intent_parsing_node(state: HotelState) -> HotelState:
    raw = state.get("raw_input")
    if not raw:
        return state

    import json
    from datetime import date

    llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)
    prompt = _INTENT_PROMPT.format(today=date.today().isoformat(), message=raw)

    response = await llm.ainvoke(prompt)
    text = response.content.strip()

    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.removeprefix("json")
    text = text.strip()

    try:
        parsed = json.loads(text)
    except Exception:
        return state

    updates: HotelState = {}
    for field in ("destination", "check_in", "check_out", "budget_per_night", "guests"):
        value = parsed.get(field)
        if value is not None and not state.get(field):
            updates[field] = value  # type: ignore[literal-required]

    return {**state, **updates}


def router(state: HotelState) -> str:
    required = ("destination", "check_in", "check_out", "budget_per_night")
    for field in required:
        if not state.get(field):
            return "clarify"
    return "search"


async def clarify_node(state: HotelState) -> HotelState:
    missing_questions = {
        "destination": "Which city would you like to stay in?",
        "check_in": "What is your check-in date?",
        "check_out": "What is your check-out date?",
        "budget_per_night": "What is your maximum budget per night for the hotel in INR?",
    }
    for field, question in missing_questions.items():
        if not state.get(field):
            return {**state, "clarification_question": question, "hotels": [], "error": None}

    return {
        **state,
        "clarification_question": "Could you provide more details about your hotel stay?",
        "hotels": [],
        "error": None,
    }


async def search_hotels_node(state: HotelState) -> HotelState:
    params = {
        "destination": state["destination"],
        "check_in": state["check_in"],
        "check_out": state["check_out"],
        "budget_per_night": state["budget_per_night"],
        "guests": state.get("guests", 1),
    }

    result = await call_tool("search_hotels", params)

    if hasattr(result, "code"):
        return {**state, "error": result.model_dump(), "hotels": []}

    return {**state, "hotels": result, "error": None}


# ── Graph ─────────────────────────────────────────────────────────────────


def build_hotel_agent_graph():
    graph = StateGraph(HotelState)

    graph.add_node("intent_parsing", intent_parsing_node)
    graph.add_node("search_hotels", search_hotels_node)
    graph.add_node("clarify", clarify_node)

    graph.set_entry_point("intent_parsing")

    graph.add_conditional_edges(
        "intent_parsing",
        router,
        {"search": "search_hotels", "clarify": "clarify"},
    )

    graph.add_edge("search_hotels", END)
    graph.add_edge("clarify", END)

    return graph.compile()


# ── Agent wrapper ─────────────────────────────────────────────────────────


class HotelAgent:
    def __init__(self):
        self._graph = build_hotel_agent_graph()

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
                agent_name="hotel_agent",
                input=input_state,
                output={"hotels": result.get("hotels", []), "error": result.get("error")},
                duration_ms=timer.duration_ms,
                status="failed" if result.get("error") is not None else "completed",
                turn=turn,
            )

        return result

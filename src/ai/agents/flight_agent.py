"""
Phase 6/7 — FlightAgent: intent parsing, routing, and flight search.
Phase 15: run() gains a `turn` parameter forwarded to log_agent_run.

Phase 6: single-node graph — structured input → search_flights via MCP.
Phase 7: three-node graph — free text → intent parse → route → search or clarify.
"""

import uuid

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypedDict

from src.ai.mcp_client.client import call_tool
from src.ai.utils.run_logger import log_agent_run, timed_run

# ── State ──────────────────────────────────────────────────────────────────


class TripState(TypedDict, total=False):
    destination: str
    origin: str
    date: str
    return_date: str | None
    budget: float
    passengers: int
    flights: list[dict]
    error: dict | None
    raw_input: str | None
    clarification_question: str | None
    conversation_history: list[dict]


# ── Prompts ────────────────────────────────────────────────────────────────

_INTENT_PROMPT = """You are a travel assistant. Extract flight search fields from the user message.

Return ONLY a JSON object with these exact keys (use null for missing values):
{{
  "origin": "<IATA code or null>",
  "destination": "<IATA code or null>",
  "date": "<YYYY-MM-DD or null>",
  "budget": <number in INR or null>,
  "passengers": <integer or null>
}}

Rules:
- Convert city names to IATA codes (Delhi=DEL, Mumbai=BOM, Goa=GOI, Bangalore=BLR)
- Convert relative dates using today as reference: {today}
- If a field is not mentioned at all, use null
- Return ONLY the JSON, no explanation

User message: {message}"""


# ── Nodes ──────────────────────────────────────────────────────────────────


async def intent_parsing_node(state: TripState) -> TripState:
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

    updates: TripState = {}
    for field in ("origin", "destination", "date", "budget", "passengers"):
        value = parsed.get(field)
        if value is not None and not state.get(field):
            updates[field] = value  # type: ignore[literal-required]

    return {**state, **updates}


def router(state: TripState) -> str:
    required = ("destination", "date", "budget")
    for field in required:
        if not state.get(field):
            return "clarify"
    return "search"


async def clarify_node(state: TripState) -> TripState:
    missing_questions = {
        "destination": "Where would you like to fly to?",
        "date": "What date would you like to travel?",
        "budget": "What is your approximate budget for flights in INR?",
    }
    for field, question in missing_questions.items():
        if not state.get(field):
            return {**state, "clarification_question": question, "flights": [], "error": None}

    return {
        **state,
        "clarification_question": "Could you provide more details about your trip?",
        "flights": [],
        "error": None,
    }


async def search_flights_node(state: TripState) -> TripState:
    params = {
        "origin": state.get("origin", "DEL"),
        "destination": state["destination"],
        "date": state["date"],
        "budget": state["budget"],
        "passengers": state.get("passengers", 1),
    }

    result = await call_tool("search_flights", params)

    if hasattr(result, "code"):
        return {**state, "error": result.model_dump(), "flights": []}

    return {**state, "flights": result, "error": None}


# ── Graph ──────────────────────────────────────────────────────────────────


def build_flight_agent_graph():
    graph = StateGraph(TripState)

    graph.add_node("intent_parsing", intent_parsing_node)
    graph.add_node("search_flights", search_flights_node)
    graph.add_node("clarify", clarify_node)

    graph.set_entry_point("intent_parsing")

    graph.add_conditional_edges(
        "intent_parsing",
        router,
        {"search": "search_flights", "clarify": "clarify"},
    )

    graph.add_edge("search_flights", END)
    graph.add_edge("clarify", END)

    return graph.compile()


# ── Agent wrapper ──────────────────────────────────────────────────────────


class FlightAgent:
    def __init__(self):
        self._graph = build_flight_agent_graph()

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
                agent_name="flight_agent",
                input=input_state,
                output={"flights": result.get("flights", []), "error": result.get("error")},
                duration_ms=timer.duration_ms,
                status="failed" if result.get("error") is not None else "completed",
                turn=turn,
            )

        return result

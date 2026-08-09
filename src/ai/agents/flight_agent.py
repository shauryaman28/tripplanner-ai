"""
Phase 6 — FlightAgent: single-node LangGraph agent.

Deliberately minimal: takes already-structured trip fields (destination,
origin, date, budget, passengers) and calls search_flights via the MCP
client. Free-text intent parsing ("somewhere warm in December") is
Phase 7's job as a separate node — keeping this node single-purpose
matches the roadmap's "one agent, one tool" framing for Phase 6.

No LLM call happens in this file. The roadmap lists Gemini Flash as the
agent's LLM, but with structured input there's nothing for an LLM to
extract yet — that need appears in Phase 7's IntentParsingNode. Adding
an unused LLM call here would just be dead weight. Flagged as a decision
in the handoff message, not silently skipped.
"""


import uuid
from src.ai.utils.run_logger import log_agent_run, timed_run

from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from src.ai.mcp_client.client import call_tool


class TripState(TypedDict, total=False):
    destination: str
    origin: str
    date: str
    return_date: Optional[str]
    budget: float
    passengers: int
    flights: list[dict]
    error: Optional[dict]


async def search_flights_node(state: TripState) -> TripState:
    """Call search_flights via the MCP client; populate flights or error."""
    params = {
        "origin": state["origin"],
        "destination": state["destination"],
        "date": state["date"],
        "budget": state["budget"],
        "passengers": state.get("passengers", 1),
    }

    result = await call_tool("search_flights", params)

    if hasattr(result, "code"):  # ToolError instance
        return {**state, "error": result.model_dump(), "flights": []}

    return {**state, "flights": result, "error": None}


def build_flight_agent_graph():
    graph = StateGraph(TripState)
    graph.add_node("search_flights", search_flights_node)
    graph.set_entry_point("search_flights")
    graph.add_edge("search_flights", END)
    return graph.compile()


class FlightAgent:
    """Thin wrapper so callers don't need to touch LangGraph directly."""

    def __init__(self):
        self._graph = build_flight_agent_graph()

    async def run(
        self,
        input_state: dict,
        db: Optional[AsyncSession] = None,
        trip_id: Optional[uuid.UUID] = None,
    ) -> dict:
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
            )

        return result


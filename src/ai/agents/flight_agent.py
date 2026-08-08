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

from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

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

    async def run(self, input_state: dict) -> dict:
        return await self._graph.ainvoke(input_state)

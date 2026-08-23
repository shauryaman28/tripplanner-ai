"""
Phase 9 — OrchestratorAgent: decomposition & fan-out.

Three-node LangGraph graph:
  1. intent_parsing_node  : Gemini Flash extracts structured TripState fields
                            from free-form input. Normalises dates and currency.
                            Policy: ask vs. assume documented in prompts/orchestrator_v3.md
  2. fan_out_node         : fires FlightAgent, HotelAgent, ActivitiesAgent
                            concurrently via asyncio.gather (not LangGraph Send,
                            which requires compiled subgraphs — gather gives the
                            same wall-clock concurrency with simpler state merge).
                            Each sub-agent receives only its relevant state slice.
  3. merge_node           : collects results into OrchestratorState. Partial
                            failure is not fatal — one agent failed → log it,
                            continue with what succeeded. Caller sees which
                            agents succeeded via the per-agent status fields.

SSE publishing:
  publish_fn is an optional async callable injected by the route so that
  the Orchestrator can stream live progress events without importing Redis
  directly (keeps the agent layer infrastructure-agnostic and testable).

  Signature: async def publish_fn(event: dict) -> None
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypedDict

from src.ai.agents.activities_agent import ActivitiesAgent
from src.ai.agents.flight_agent import FlightAgent
from src.ai.agents.hotel_agent import HotelAgent
from src.ai.utils.run_logger import log_agent_run, timed_run


# ── State ─────────────────────────────────────────────────────────────────


class OrchestratorState(TypedDict, total=False):
    # ── Raw input (optional — for free-text queries) ───────────────────
    raw_input: str | None

    # ── Extracted trip fields ──────────────────────────────────────────
    destination: str | None
    origin: str | None               # default DEL if absent
    start_date: str | None           # ISO date
    end_date: str | None             # ISO date
    budget: float | None             # total trip budget INR
    group_size: int | None           # number of travellers (default 1)
    interests: list[str] | None      # activity interests

    # ── Sub-agent results ──────────────────────────────────────────────
    flights: list[dict]
    hotels: list[dict]
    attractions: list[dict]

    # ── Per-agent status ("completed" | "failed" | "skipped") ─────────
    flight_status: str
    hotel_status: str
    activities_status: str

    # ── Errors ────────────────────────────────────────────────────────
    flight_error: dict | None
    hotel_error: dict | None
    activities_error: dict | None

    # ── Runtime helpers (not stored in DB) ────────────────────────────
    publish_fn: Any | None   # async callable for SSE progress events
    db: Any | None           # AsyncSession (injected by route)
    trip_id: Any | None      # uuid.UUID


# ── Intent parsing prompt ─────────────────────────────────────────────────

_INTENT_PROMPT = """You are an expert travel planning assistant. Extract structured trip details from the user message.

Return ONLY a valid JSON object with these exact keys (use null for missing values):
{{
  "destination": "<city name or null>",
  "origin": "<city name or null>",
  "start_date": "<YYYY-MM-DD or null>",
  "end_date": "<YYYY-MM-DD or null>",
  "budget": <total budget as a number in INR or null>,
  "group_size": <number of people as integer or null>,
  "interests": ["list", "of", "activity", "keywords"] or null
}}

Rules:
- Today is {today}. Convert all relative dates (\"next month\", \"in December\", \"for 5 days from Jan 10\") to absolute ISO dates.
- Convert vague date ranges: \"7 days\" from a known start → compute end_date. If only duration given with no start, use null for both.
- Budget normalisation: convert \"50k\" → 50000, \"2 lakhs\" → 200000. Always store as INR integer. Never split budget by sub-category — store the TOTAL trip budget.
- group_size: \"solo\" → 1, \"couple\" → 2, \"family of 4\" → 4. If not mentioned, use null (caller defaults to 1).
- interests: extract as short English keyword phrases (e.g. \"beach\", \"history\", \"street food\", \"adventure\"). Translate non-English interest words to English.
- origin: if not mentioned, use null (caller defaults to \"DEL\").
- Ask vs. assume policy: if destination is completely absent, set it to null — do NOT guess. Ambiguous dates (\"sometime in December\") → set start_date to first day of mentioned month, end_date to null.
- Return ONLY the JSON object. No explanation, no markdown fences, no preamble.

User message: {message}"""


# ── Nodes ─────────────────────────────────────────────────────────────────


async def intent_parsing_node(state: OrchestratorState) -> OrchestratorState:
    """Extract structured trip fields from free-form text via Gemini Flash.

    If raw_input is absent (caller already provided structured fields),
    passes state through unchanged — backward-compatible with structured callers.
    Normalises dates, currency, and group size per the ask-vs-assume policy.
    """
    raw = state.get("raw_input")
    if not raw:
        return state

    import json
    from datetime import date

    llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)
    prompt = _INTENT_PROMPT.format(today=date.today().isoformat(), message=raw)

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
        # unparseable → leave state as-is; sub-agents may clarify
        return state

    updates: OrchestratorState = {}
    field_map = {
        "destination": "destination",
        "origin": "origin",
        "start_date": "start_date",
        "end_date": "end_date",
        "budget": "budget",
        "group_size": "group_size",
        "interests": "interests",
    }
    for json_key, state_key in field_map.items():
        value = parsed.get(json_key)
        if value is not None and not state.get(state_key):
            updates[state_key] = value  # type: ignore[literal-required]

    return {**state, **updates}


async def fan_out_node(state: OrchestratorState) -> OrchestratorState:
    """Run FlightAgent, HotelAgent, and ActivitiesAgent concurrently.

    Each sub-agent receives only the state slice it needs. Results are
    collected into OrchestratorState. One agent failing never blocks the
    other two — asyncio.gather(return_exceptions=True) ensures all three
    always complete.

    SSE progress events are published after each agent finishes via the
    injected publish_fn.
    """
    publish_fn = state.get("publish_fn")
    db = state.get("db")
    trip_id = state.get("trip_id")

    destination = state.get("destination", "")
    origin = state.get("origin") or "DEL"
    start_date = state.get("start_date", "")
    end_date = state.get("end_date", "")
    budget = state.get("budget", 0.0)
    group_size = state.get("group_size") or 1
    interests = state.get("interests") or []

    # ── Build sub-agent input slices ───────────────────────────────────
    flight_input = {
        "destination": destination,
        "origin": origin,
        "date": start_date,
        "return_date": end_date or None,
        "budget": budget,
        "passengers": group_size,
    }
    hotel_input = {
        "destination": destination,
        "check_in": start_date,
        "check_out": end_date,
        "budget_per_night": round(budget / max(1, (group_size or 1)) / 7, 2),
        "guests": group_size,
    }
    activities_input = {
        "destination": destination,
        "interests": interests,
        "limit": 5,
    }

    # ── Run concurrently ───────────────────────────────────────────────
    results = await asyncio.gather(
        FlightAgent().run(flight_input, db=db, trip_id=trip_id),
        HotelAgent().run(hotel_input, db=db, trip_id=trip_id),
        ActivitiesAgent().run(activities_input, db=db, trip_id=trip_id),
        return_exceptions=True,
    )

    flight_result, hotel_result, activities_result = results

    # ── Process flight result ──────────────────────────────────────────
    if isinstance(flight_result, Exception):
        flights, flight_error, flight_status = [], {"error": str(flight_result), "code": "AGENT_EXCEPTION"}, "failed"
    elif flight_result.get("error"):
        flights, flight_error, flight_status = [], flight_result["error"], "failed"
    else:
        flights, flight_error, flight_status = flight_result.get("flights", []), None, "completed"

    # ── Process hotel result ───────────────────────────────────────────
    if isinstance(hotel_result, Exception):
        hotels, hotel_error, hotel_status = [], {"error": str(hotel_result), "code": "AGENT_EXCEPTION"}, "failed"
    elif hotel_result.get("error"):
        hotels, hotel_error, hotel_status = [], hotel_result["error"], "failed"
    else:
        hotels, hotel_error, hotel_status = hotel_result.get("hotels", []), None, "completed"

    # ── Process activities result ──────────────────────────────────────
    if isinstance(activities_result, Exception):
        attractions, activities_error, activities_status = [], {"error": str(activities_result), "code": "AGENT_EXCEPTION"}, "failed"
    elif activities_result.get("error"):
        attractions, activities_error, activities_status = [], activities_result["error"], "failed"
    else:
        attractions, activities_error, activities_status = activities_result.get("attractions", []), None, "completed"

    # ── Publish SSE progress events ────────────────────────────────────
    if publish_fn:
        for agent_name, status, summary, error in [
            ("flight_agent", flight_status, f"Found {len(flights)} flights", flight_error),
            ("hotel_agent", hotel_status, f"Found {len(hotels)} hotels", hotel_error),
            ("activities_agent", activities_status, f"Found {len(attractions)} attractions", activities_error),
        ]:
            try:
                await publish_fn({
                    "agent": agent_name,
                    "status": status,
                    "summary": summary if status == "completed" else (error or {}).get("error", "Failed"),
                })
            except Exception:
                pass  # SSE publish failure never blocks planning

    return {
        **state,
        "flights": flights,
        "flight_error": flight_error,
        "flight_status": flight_status,
        "hotels": hotels,
        "hotel_error": hotel_error,
        "hotel_status": hotel_status,
        "attractions": attractions,
        "activities_error": activities_error,
        "activities_status": activities_status,
    }


async def merge_node(state: OrchestratorState) -> OrchestratorState:
    """Validate the merged state and publish planning_complete event.

    Partial failure is acceptable — if at least one agent succeeded,
    the orchestration is considered successful enough to proceed to the
    ItineraryBuilder (Phase 12). Full failure (all three agents failed)
    is surfaced via the status fields for the route to handle.
    """
    publish_fn = state.get("publish_fn")

    agents_done = sum(
        1 for s in [state.get("flight_status"), state.get("hotel_status"), state.get("activities_status")]
        if s == "completed"
    )
    agents_total = 3

    if publish_fn:
        try:
            await publish_fn({
                "event": "planning_complete",
                "agents_done": agents_done,
                "agents_total": agents_total,
                "status": "completed" if agents_done > 0 else "failed",
            })
        except Exception:
            pass

    return state


# ── Graph ─────────────────────────────────────────────────────────────────


def build_orchestrator_graph():
    graph = StateGraph(OrchestratorState)

    graph.add_node("intent_parsing", intent_parsing_node)
    graph.add_node("fan_out", fan_out_node)
    graph.add_node("merge", merge_node)

    graph.set_entry_point("intent_parsing")
    graph.add_edge("intent_parsing", "fan_out")
    graph.add_edge("fan_out", "merge")
    graph.add_edge("merge", END)

    return graph.compile()


# ── Agent wrapper ─────────────────────────────────────────────────────────


class OrchestratorAgent:
    """Thin wrapper so callers (routes) don't need to touch LangGraph directly."""

    def __init__(self):
        self._graph = build_orchestrator_graph()

    async def run(
        self,
        input_state: dict,
        db: AsyncSession | None = None,
        trip_id: uuid.UUID | None = None,
        publish_fn=None,
    ) -> dict:
        # Inject runtime helpers into state
        full_state = {
            **input_state,
            "db": db,
            "trip_id": trip_id,
            "publish_fn": publish_fn,
            # Ensure list/error fields have defaults so merge_node never KeyErrors
            "flights": [],
            "hotels": [],
            "attractions": [],
            "flight_status": "skipped",
            "hotel_status": "skipped",
            "activities_status": "skipped",
            "flight_error": None,
            "hotel_error": None,
            "activities_error": None,
        }

        async with timed_run() as timer:
            result = await self._graph.ainvoke(full_state)

        # Write orchestrator-level agent_run row
        if db is not None and trip_id is not None:
            agents_done = sum(
                1 for s in [result.get("flight_status"), result.get("hotel_status"), result.get("activities_status")]
                if s == "completed"
            )
            await log_agent_run(
                db=db,
                trip_id=trip_id,
                agent_name="orchestrator",
                input={k: v for k, v in input_state.items() if k not in ("db", "trip_id", "publish_fn")},
                output={
                    "flights_count": len(result.get("flights", [])),
                    "hotels_count": len(result.get("hotels", [])),
                    "attractions_count": len(result.get("attractions", [])),
                    "flight_status": result.get("flight_status"),
                    "hotel_status": result.get("hotel_status"),
                    "activities_status": result.get("activities_status"),
                },
                duration_ms=timer.duration_ms,
                status="completed" if agents_done > 0 else "failed",
            )

        # Strip non-serialisable runtime helpers before returning
        clean = {k: v for k, v in result.items() if k not in ("db", "trip_id", "publish_fn")}
        return clean

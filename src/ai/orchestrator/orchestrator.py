"""
Phase 10 + 12 — OrchestratorAgent: Budget Conflict, Re-Planning, and
Itinerary Build/Evaluate/Persist.

Graph structure:

    intent_parsing_node
          ↓
    run_flight_node         ← FlightAgent only
          ↓
    budget_decision_node    ← pure budget check + DB log
          ↓ (conditional edge: route_after_budget_decision)
    ┌─── "continue" ──────→ hotel_activities_node
    ├─── "replan"   ──────→ run_flight_node (loop, cap = MAX_REPLAN_ATTEMPTS)
    └─── "escalate" ──────→ escalate_node → END

    hotel_activities_node
          ↓
    build_itinerary_node    ← ItineraryBuilder (Claude Haiku), Phase 12
          ↓
    evaluate_node           ← EvaluatorAgent, Phase 11 — now wired in
          ↓ (conditional edge: route_after_evaluator)
    ┌─── "passed" ─────────→ persist_node → merge_node → END
    ├─── "retry"  ─────────→ retry_dispatch_node → build_itinerary_node (loop, cap = MAX_EVALUATOR_RETRIES)
    └─── "failed" ─────────→ builder_failed_node → END

SSE events (happy path):
    planning_started → flight_agent → hotel_agent → activities_agent → planning_complete

SSE events (evaluator exhausts retries):
    planning_started → ... → planning_failed (agent: itinerary_builder)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date
from typing import Any

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypedDict

from src.ai.agents.activities_agent import ActivitiesAgent
from src.ai.agents.budget_decision import (
    make_budget_decision,
    replan_flight_budget,
)
from src.ai.agents.evaluator import (
    MAX_EVALUATOR_RETRIES,
    EvaluatorAgent,
    EvaluatorFailure,
    EvaluatorVerdict,
    next_agent_for_failures,
    route_after_evaluation,
)
from src.ai.agents.flight_agent import FlightAgent
from src.ai.agents.hotel_agent import HotelAgent
from src.ai.builder.builder import ItineraryBuilder
from src.ai.utils.embeddings import generate_embeddings
from src.ai.utils.run_logger import log_agent_run, timed_run

try:
    from app.models.itinerary import Itinerary
    from app.models.trip import Trip, TripStatus
except ImportError:
    from src.backend.app.models.itinerary import Itinerary
    from src.backend.app.models.trip import Trip, TripStatus


# ── State ─────────────────────────────────────────────────────────────────


class OrchestratorState(TypedDict, total=False):
    # ── Raw input (optional — free-text queries) ───────────────────────
    raw_input: str | None

    # ── Extracted trip fields ──────────────────────────────────────────
    destination: str | None
    origin: str | None
    start_date: str | None
    end_date: str | None
    budget: float | None
    group_size: int | None
    interests: list[str] | None

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

    # ── Phase 10: budget conflict & re-planning ────────────────────────
    replan_attempts: int
    budget_decision: dict | None
    budget_conflict_options: list[dict] | None

    # ── Phase 12: build / evaluate / persist ───────────────────────────
    draft_itinerary: dict | None
    builder_error: dict | None
    evaluator_verdict: dict | None
    evaluator_retry_count: int
    itinerary_id: Any | None

    # ── Runtime helpers (never stored in DB) ──────────────────────────
    publish_fn: Any | None
    db: Any | None
    trip_id: Any | None


# ── Intent parsing prompt (unchanged from Phase 10) ────────────────────────

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
- Today is {today}. Convert all relative dates to absolute ISO dates.
- Budget normalisation: convert \"50k\" → 50000, \"2 lakhs\" → 200000. Always store as INR integer.
- group_size: \"solo\" → 1, \"couple\" → 2, \"family of 4\" → 4. If not mentioned, use null.
- interests: extract as short English keyword phrases. Translate non-English to English.
- origin: if not mentioned, use null (caller defaults to \"DEL\").
- destination absent → set null, do NOT guess.
- Return ONLY the JSON object. No explanation, no markdown fences.

User message: {message}"""


# ── Nodes (Phase 9/10 — unchanged) ──────────────────────────────────────────


async def intent_parsing_node(state: OrchestratorState) -> OrchestratorState:
    raw = state.get("raw_input")
    if not raw:
        return state

    import json

    llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)
    prompt = _INTENT_PROMPT.format(today=date.today().isoformat(), message=raw)

    response = await llm.ainvoke(prompt)
    text = response.content.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()

    try:
        parsed = json.loads(text)
    except Exception:
        return state

    updates: OrchestratorState = {}
    for key in ("destination", "origin", "start_date", "end_date", "budget", "group_size", "interests"):
        value = parsed.get(key)
        if value is not None and not state.get(key):
            updates[key] = value  # type: ignore[literal-required]

    return {**state, **updates}


async def run_flight_node(state: OrchestratorState) -> OrchestratorState:
    publish_fn = state.get("publish_fn")
    db = state.get("db")
    trip_id = state.get("trip_id")

    replan_attempts = state.get("replan_attempts", 0)
    total_budget = state.get("budget") or 0.0
    flight_budget = replan_flight_budget(total_budget, replan_attempts) if replan_attempts > 0 else total_budget

    flight_input = {
        "destination": state.get("destination", ""),
        "origin": state.get("origin") or "DEL",
        "date": state.get("start_date", ""),
        "return_date": state.get("end_date") or None,
        "budget": flight_budget,
        "passengers": state.get("group_size") or 1,
    }

    result = await FlightAgent().run(flight_input, db=db, trip_id=trip_id)

    if result.get("error"):
        flights, flight_error, flight_status = [], result["error"], "failed"
    else:
        flights, flight_error, flight_status = result.get("flights", []), None, "completed"

    if publish_fn and flight_status == "completed":
        try:
            summary = f"Found {len(flights)} flights"
            if replan_attempts > 0:
                summary += f" (re-plan attempt {replan_attempts})"
            await publish_fn({"agent": "flight_agent", "status": flight_status, "summary": summary})
        except Exception:
            pass

    return {**state, "flights": flights, "flight_error": flight_error, "flight_status": flight_status}


async def budget_decision_node(state: OrchestratorState) -> OrchestratorState:
    db = state.get("db")
    trip_id = state.get("trip_id")

    flights = state.get("flights", [])
    total_budget = state.get("budget") or 0.0
    replan_attempts = state.get("replan_attempts", 0)

    start = time.monotonic()
    decision = make_budget_decision(flights, total_budget, replan_attempts)
    duration_ms = int((time.monotonic() - start) * 1000)

    budget_conflict_options: list[dict] | None = None
    if decision.decision == "escalate":
        budget_conflict_options = [
            {
                "choice": "cheaper_flights",
                "description": "Search for cheaper connecting flights",
                "estimated_saving": f"₹{decision.flight_cost * 0.35:,.0f}",
            },
            {
                "choice": "reduce_days",
                "description": "Shorten the trip by 2 days to reduce hotel costs",
                "estimated_saving": "~₹8,000–15,000",
            },
            {
                "choice": "increase_budget",
                "description": "Increase total budget by 25%",
                "estimated_saving": f"Additional ₹{total_budget * 0.25:,.0f}",
            },
        ]

    if db is not None and trip_id is not None:
        await log_agent_run(
            db=db,
            trip_id=trip_id,
            agent_name="budget_decision",
            input={
                "flights_evaluated": len(flights),
                "cheapest_flight": decision.flight_cost,
                "total_budget": total_budget,
                "replan_attempts": replan_attempts,
            },
            output=decision.model_dump(),
            duration_ms=duration_ms,
            status="completed",
        )

    return {
        **state,
        "budget_decision": decision.model_dump(),
        "budget_conflict_options": budget_conflict_options,
        "replan_attempts": replan_attempts + (1 if decision.decision == "replan" else 0),
    }


def route_after_budget_decision(state: OrchestratorState) -> str:
    bd = state.get("budget_decision")
    if not bd:
        return "continue"
    return bd.get("decision", "continue")


async def hotel_activities_node(state: OrchestratorState) -> OrchestratorState:
    publish_fn = state.get("publish_fn")
    db = state.get("db")
    trip_id = state.get("trip_id")

    destination = state.get("destination", "")
    start_date = state.get("start_date", "")
    end_date = state.get("end_date", "")
    group_size = state.get("group_size") or 1
    interests = state.get("interests") or []

    bd = state.get("budget_decision") or {}
    remaining_budget = bd.get("remaining_budget") or (state.get("budget") or 0.0)
    try:
        days = max(1, (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days)
    except Exception:
        days = 7
    budget_per_night = round(remaining_budget / days, 2)

    hotel_input = {
        "destination": destination,
        "check_in": start_date,
        "check_out": end_date,
        "budget_per_night": budget_per_night,
        "guests": group_size,
    }
    activities_input = {
        "destination": destination,
        "interests": interests,
        "limit": 5,
    }

    results = await asyncio.gather(
        HotelAgent().run(hotel_input, db=db, trip_id=trip_id),
        ActivitiesAgent().run(activities_input, db=db, trip_id=trip_id),
        return_exceptions=True,
    )
    hotel_result, activities_result = results

    if isinstance(hotel_result, Exception):
        hotels, hotel_error, hotel_status = [], {"error": str(hotel_result), "code": "AGENT_EXCEPTION"}, "failed"
    elif hotel_result.get("error"):
        hotels, hotel_error, hotel_status = [], hotel_result["error"], "failed"
    else:
        hotels, hotel_error, hotel_status = hotel_result.get("hotels", []), None, "completed"

    if isinstance(activities_result, Exception):
        attractions, activities_error, activities_status = (
            [],
            {"error": str(activities_result), "code": "AGENT_EXCEPTION"},
            "failed",
        )
    elif activities_result.get("error"):
        attractions, activities_error, activities_status = [], activities_result["error"], "failed"
    else:
        attractions, activities_error, activities_status = activities_result.get("attractions", []), None, "completed"

    if publish_fn:
        for agent_name, status, summary, error in [
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
                pass

    return {
        **state,
        "hotels": hotels,
        "hotel_error": hotel_error,
        "hotel_status": hotel_status,
        "attractions": attractions,
        "activities_error": activities_error,
        "activities_status": activities_status,
    }


async def escalate_node(state: OrchestratorState) -> OrchestratorState:
    publish_fn = state.get("publish_fn")
    db = state.get("db")
    trip_id = state.get("trip_id")
    bd = state.get("budget_decision") or {}
    options = state.get("budget_conflict_options") or []

    if db is not None and trip_id is not None:
        trip = await db.get(Trip, trip_id)
        if trip is not None:
            trip.status = TripStatus.FAILED
            db.add(trip)
            await db.commit()

    if publish_fn:
        try:
            await publish_fn({
                "event": "budget_conflict",
                "reason": bd.get("reason", "Flights exceed available budget."),
                "flight_cost": bd.get("flight_cost", 0),
                "remaining_budget": bd.get("remaining_budget", 0),
                "options": options,
            })
            await publish_fn({
                "event": "planning_failed",
                "agent": "orchestrator",
                "status": "failed",
                "error": "budget_conflict",
            })
        except Exception:
            pass

    return {**state, "hotel_status": "skipped", "activities_status": "skipped"}


# ── Nodes (Phase 12 — build / evaluate / persist) ───────────────────────────


async def build_itinerary_node(state: OrchestratorState) -> OrchestratorState:
    """Run ItineraryBuilder (Claude Haiku) against the sub-agents' data."""
    db = state.get("db")
    trip_id = state.get("trip_id")

    trip_meta = {
        "destination": state.get("destination", ""),
        "start_date": state.get("start_date", ""),
        "end_date": state.get("end_date", ""),
        "group_size": state.get("group_size") or 1,
    }

    result = await ItineraryBuilder().run(
        trip_meta=trip_meta,
        flights=state.get("flights", []),
        hotels=state.get("hotels", []),
        attractions=state.get("attractions", []),
        db=db,
        trip_id=trip_id,
    )

    return {**state, "draft_itinerary": result.get("draft"), "builder_error": result.get("error")}


async def evaluate_node(state: OrchestratorState) -> OrchestratorState:
    """Run EvaluatorAgent (Phase 11) against the builder's draft."""
    db = state.get("db")
    trip_id = state.get("trip_id")
    draft = state.get("draft_itinerary")
    retry_count = state.get("evaluator_retry_count", 0)

    if draft is None:
        # Builder itself failed to produce a draft — nothing to evaluate.
        # route_after_evaluator handles this case directly via builder_error.
        return {**state, "evaluator_verdict": {"passed": False, "failures": [], "retry_count": retry_count}}

    verdict = await EvaluatorAgent().run(
        draft=draft,
        trip_start=state.get("start_date", ""),
        trip_end=state.get("end_date", ""),
        expected_budget_total=state.get("budget") or 0.0,
        attractions=state.get("attractions", []),
        retry_count=retry_count,
        db=db,
        trip_id=trip_id,
    )
    return {**state, "evaluator_verdict": verdict.model_dump()}


def route_after_evaluator(state: OrchestratorState) -> str:
    """Conditional edge: 'passed' | 'retry' | 'failed'.

    Builder failures (no draft at all) share the same retry cap as
    Evaluator failures — one bounded loop for the whole build+evaluate
    stage, rather than two independent caps.
    """
    if state.get("builder_error") is not None and state.get("draft_itinerary") is None:
        retry_count = state.get("evaluator_retry_count", 0)
        return "failed" if retry_count >= MAX_EVALUATOR_RETRIES else "retry"

    verdict_dict = state.get("evaluator_verdict") or {"passed": False, "failures": [], "retry_count": 0}
    verdict = EvaluatorVerdict(**verdict_dict)
    return route_after_evaluation(verdict)


async def retry_dispatch_node(state: OrchestratorState) -> OrchestratorState:
    """Re-run the sub-agent implicated by the Evaluator's failures, then loop
    back to build_itinerary_node. Increments evaluator_retry_count."""
    db = state.get("db")
    trip_id = state.get("trip_id")
    verdict_dict = state.get("evaluator_verdict") or {}
    retry_count = state.get("evaluator_retry_count", 0) + 1

    failures = verdict_dict.get("failures", [])
    if failures:
        agent_to_retry = next_agent_for_failures([EvaluatorFailure(**f) for f in failures])
    else:
        # Builder-level failure (no verdict) — regenerate activities as the
        # most likely source of a bad draft (missing/renamed data).
        agent_to_retry = "activities_agent"

    new_state: OrchestratorState = {**state, "evaluator_retry_count": retry_count}

    if agent_to_retry == "flight_agent":
        flight_result = await FlightAgent().run(
            {
                "destination": state.get("destination", ""),
                "origin": state.get("origin") or "DEL",
                "date": state.get("start_date", ""),
                "return_date": state.get("end_date") or None,
                "budget": state.get("budget") or 0.0,
                "passengers": state.get("group_size") or 1,
            },
            db=db,
            trip_id=trip_id,
        )
        if not flight_result.get("error"):
            new_state["flights"] = flight_result.get("flights", [])
    else:
        activities_result = await ActivitiesAgent().run(
            {
                "destination": state.get("destination", ""),
                "interests": state.get("interests") or [],
                "limit": 5,
            },
            db=db,
            trip_id=trip_id,
        )
        if not activities_result.get("error"):
            new_state["attractions"] = activities_result.get("attractions", [])

    return new_state


async def persist_node(state: OrchestratorState) -> OrchestratorState:
    """Write the itinerary row + mark the trip completed — one commit, atomic.

    Both writes share a single AsyncSession and a single commit() call: if
    anything raises before commit, neither write lands (Phase 12 Dev B
    acceptance criterion — "forced mid-write failure → neither row persists").
    """
    db = state.get("db")
    trip_id = state.get("trip_id")
    draft = state.get("draft_itinerary") or {}

    if db is None or trip_id is None:
        return state

    itinerary = Itinerary(
        trip_id=trip_id,
        structured_data=draft,
        total_cost=draft.get("total_cost"),
    )
    trip = await db.get(Trip, trip_id)
    if trip is not None:
        trip.status = TripStatus.COMPLETED
        db.add(trip)
    db.add(itinerary)

    await db.commit()
    await db.refresh(itinerary)

    try:
        await generate_embeddings(itinerary.id)
    except Exception:
        pass  # embedding is best-effort — never fails the planning run

    return {**state, "itinerary_id": itinerary.id}


async def builder_failed_node(state: OrchestratorState) -> OrchestratorState:
    """Evaluator/Builder retries exhausted — mark the trip failed honestly."""
    db = state.get("db")
    trip_id = state.get("trip_id")
    publish_fn = state.get("publish_fn")

    if db is not None and trip_id is not None:
        trip = await db.get(Trip, trip_id)
        if trip is not None:
            trip.status = TripStatus.FAILED
            db.add(trip)
            await db.commit()

    if publish_fn:
        try:
            error_detail = (state.get("builder_error") or {}).get(
                "error", "Itinerary could not be built after maximum retries."
            )
            await publish_fn({
                "event": "planning_failed",
                "agent": "itinerary_builder",
                "status": "failed",
                "error": error_detail,
            })
        except Exception:
            pass

    return state


async def merge_node(state: OrchestratorState) -> OrchestratorState:
    """Publish planning_complete. Called only on the fully-persisted path."""
    publish_fn = state.get("publish_fn")
    agents_done = sum(
        1 for s in [state.get("flight_status"), state.get("hotel_status"), state.get("activities_status")]
        if s == "completed"
    )
    if publish_fn:
        try:
            await publish_fn({
                "event": "planning_complete",
                "agents_done": agents_done,
                "agents_total": 3,
                "itinerary_id": str(state["itinerary_id"]) if state.get("itinerary_id") else None,
                "status": "completed" if state.get("itinerary_id") else "failed",
            })
        except Exception:
            pass
    return state


# ── Graph ─────────────────────────────────────────────────────────────────


def build_orchestrator_graph():
    graph = StateGraph(OrchestratorState)

    graph.add_node("intent_parsing", intent_parsing_node)
    graph.add_node("run_flight", run_flight_node)
    graph.add_node("budget_decision", budget_decision_node)
    graph.add_node("hotel_activities", hotel_activities_node)
    graph.add_node("build_itinerary", build_itinerary_node)
    graph.add_node("evaluate", evaluate_node)
    graph.add_node("retry_dispatch", retry_dispatch_node)
    graph.add_node("persist", persist_node)
    graph.add_node("builder_failed", builder_failed_node)
    graph.add_node("merge", merge_node)
    graph.add_node("escalate", escalate_node)

    graph.set_entry_point("intent_parsing")
    graph.add_edge("intent_parsing", "run_flight")
    graph.add_edge("run_flight", "budget_decision")
    graph.add_conditional_edges(
        "budget_decision",
        route_after_budget_decision,
        {
            "continue": "hotel_activities",
            "replan": "run_flight",
            "escalate": "escalate",
        },
    )
    graph.add_edge("hotel_activities", "build_itinerary")
    graph.add_edge("build_itinerary", "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        route_after_evaluator,
        {
            "passed": "persist",
            "retry": "retry_dispatch",
            "failed": "builder_failed",
        },
    )
    graph.add_edge("retry_dispatch", "build_itinerary")
    graph.add_edge("persist", "merge")
    graph.add_edge("merge", END)
    graph.add_edge("builder_failed", END)
    graph.add_edge("escalate", END)

    return graph.compile()


# ── Agent wrapper ─────────────────────────────────────────────────────────


class OrchestratorAgent:
    """Thin wrapper so callers don't need to touch LangGraph directly."""

    def __init__(self):
        self._graph = build_orchestrator_graph()

    async def run(
        self,
        input_state: dict,
        db: AsyncSession | None = None,
        trip_id: uuid.UUID | None = None,
        publish_fn=None,
    ) -> dict:
        full_state = {
            **input_state,
            "db": db,
            "trip_id": trip_id,
            "publish_fn": publish_fn,
            "flights": [],
            "hotels": [],
            "attractions": [],
            "flight_status": "skipped",
            "hotel_status": "skipped",
            "activities_status": "skipped",
            "flight_error": None,
            "hotel_error": None,
            "activities_error": None,
            "replan_attempts": 0,
            "budget_decision": None,
            "budget_conflict_options": None,
            "draft_itinerary": None,
            "builder_error": None,
            "evaluator_verdict": None,
            "evaluator_retry_count": 0,
            "itinerary_id": None,
        }

        async with timed_run() as timer:
            result = await self._graph.ainvoke(full_state)

        if db is not None and trip_id is not None:
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
                    "budget_decision": result.get("budget_decision"),
                    "replan_attempts": result.get("replan_attempts", 0),
                    "evaluator_retry_count": result.get("evaluator_retry_count", 0),
                    "itinerary_id": str(result["itinerary_id"]) if result.get("itinerary_id") else None,
                },
                duration_ms=timer.duration_ms,
                status="completed" if result.get("itinerary_id") else "failed",
            )

        return {k: v for k, v in result.items() if k not in ("db", "trip_id", "publish_fn")}

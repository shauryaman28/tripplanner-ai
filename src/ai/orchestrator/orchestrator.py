"""
Phase 10 + 12 + 13 — OrchestratorAgent.

Phase 13 adds agent_runs instrumentation to the four previously silent
decision nodes: intent_parsing_node, persist_node, escalate_node, and
builder_failed_node. Every node that makes a decision now has a row —
the only exemption is merge_node (a pure publish step, no decision).

Graph structure (unchanged from Phase 12):

    intent_parsing_node       ← NOW LOGGED (Phase 13)
          ↓
    run_flight_node
          ↓
    budget_decision_node
          ↓ (conditional: route_after_budget_decision)
    ┌─── "continue" ──────→ hotel_activities_node
    ├─── "replan"   ──────→ run_flight_node (loop, cap = MAX_REPLAN_ATTEMPTS)
    └─── "escalate" ──────→ escalate_node → END   ← NOW LOGGED (Phase 13)

    hotel_activities_node
          ↓
    build_itinerary_node
          ↓
    evaluate_node
          ↓ (conditional: route_after_evaluator)
    ┌─── "passed" ─────────→ persist_node → merge_node → END  ← persist NOW LOGGED
    ├─── "retry"  ─────────→ retry_dispatch_node → build_itinerary_node
    └─── "failed" ─────────→ builder_failed_node → END        ← NOW LOGGED

Minimum agent_runs rows on a full happy-path run (Phase 13 acceptance criterion):
    intent_parsing   1
    flight_agent     1   (via FlightAgent.run)
    budget_decision  1
    hotel_agent      1   (via HotelAgent.run)
    activities_agent 1   (via ActivitiesAgent.run)
    itinerary_builder 1  (via ItineraryBuilder.run)
    evaluator        1   (via EvaluatorAgent.run)
    persist          1
    orchestrator     1   (OrchestratorAgent.run wrapper)
    ─────────────────
    total ≥ 9           (roadmap requires ≥ 7)
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
    raw_input: str | None

    destination: str | None
    origin: str | None
    start_date: str | None
    end_date: str | None
    budget: float | None
    group_size: int | None
    interests: list[str] | None

    flights: list[dict]
    hotels: list[dict]
    attractions: list[dict]

    flight_status: str
    hotel_status: str
    activities_status: str

    flight_error: dict | None
    hotel_error: dict | None
    activities_error: dict | None

    replan_attempts: int
    budget_decision: dict | None
    budget_conflict_options: list[dict] | None

    draft_itinerary: dict | None
    builder_error: dict | None
    evaluator_verdict: dict | None
    evaluator_retry_count: int
    itinerary_id: Any | None

    # Runtime helpers — never stored in DB
    publish_fn: Any | None
    db: Any | None
    trip_id: Any | None


# ── Intent parsing prompt ──────────────────────────────────────────────────

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


# ── Nodes ─────────────────────────────────────────────────────────────────


async def intent_parsing_node(state: OrchestratorState) -> OrchestratorState:
    """Extract trip fields from free-form text via Gemini Flash.

    Phase 13: logs one agent_runs row per invocation so the full pipeline
    trace includes the LLM extraction step.  When raw_input is absent the
    node is still logged (as a pass-through) so callers can see it ran.
    """
    db = state.get("db")
    trip_id = state.get("trip_id")
    raw = state.get("raw_input")

    async with timed_run() as timer:
        if not raw:
            result_state = state
            extracted: dict = {}
        else:
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
                parsed = {}

            updates: OrchestratorState = {}
            for key in ("destination", "origin", "start_date", "end_date", "budget", "group_size", "interests"):
                value = parsed.get(key)
                if value is not None and not state.get(key):
                    updates[key] = value  # type: ignore[literal-required]

            extracted = updates
            result_state = {**state, **updates}

    if db is not None and trip_id is not None:
        await log_agent_run(
            db=db,
            trip_id=trip_id,
            agent_name="intent_parsing",
            input={"raw_input": raw},
            output={"extracted_fields": extracted, "pass_through": not bool(raw)},
            duration_ms=timer.duration_ms,
            status="completed",
        )

    return result_state


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
            [], {"error": str(activities_result), "code": "AGENT_EXCEPTION"}, "failed",
        )
    elif activities_result.get("error"):
        attractions, activities_error, activities_status = [], activities_result["error"], "failed"
    else:
        attractions, activities_error, activities_status = activities_result.get("attractions", []), None, "completed"

    if publish_fn:
        for agent_name, ag_status, summary, error in [
            ("hotel_agent", hotel_status, f"Found {len(hotels)} hotels", hotel_error),
            ("activities_agent", activities_status, f"Found {len(attractions)} attractions", activities_error),
        ]:
            try:
                await publish_fn({
                    "agent": agent_name,
                    "status": ag_status,
                    "summary": summary if ag_status == "completed" else (error or {}).get("error", "Failed"),
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
    """Publish budget_conflict SSE and mark trip failed.

    Phase 13: now logs an agent_runs row so the escalation decision is
    visible in GET /trips/{id}/runs and the timeline endpoint.
    """
    publish_fn = state.get("publish_fn")
    db = state.get("db")
    trip_id = state.get("trip_id")
    bd = state.get("budget_decision") or {}
    options = state.get("budget_conflict_options") or []

    async with timed_run() as timer:
        if db is not None and trip_id is not None:
            trip = await db.get(Trip, trip_id)
            if trip is not None:
                trip.status = TripStatus.FAILED
                db.add(trip)
                await db.commit()

    if db is not None and trip_id is not None:
        await log_agent_run(
            db=db,
            trip_id=trip_id,
            agent_name="escalate",
            input={
                "flight_cost": bd.get("flight_cost", 0),
                "remaining_budget": bd.get("remaining_budget", 0),
                "total_budget": bd.get("total_budget", 0),
            },
            output={
                "reason": bd.get("reason", ""),
                "options_offered": len(options),
                "trip_status": "failed",
            },
            duration_ms=timer.duration_ms,
            status="completed",
        )

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


async def build_itinerary_node(state: OrchestratorState) -> OrchestratorState:
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
    db = state.get("db")
    trip_id = state.get("trip_id")
    draft = state.get("draft_itinerary")
    retry_count = state.get("evaluator_retry_count", 0)

    if draft is None:
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
    if state.get("builder_error") is not None and state.get("draft_itinerary") is None:
        retry_count = state.get("evaluator_retry_count", 0)
        return "failed" if retry_count >= MAX_EVALUATOR_RETRIES else "retry"

    verdict_dict = state.get("evaluator_verdict") or {"passed": False, "failures": [], "retry_count": 0}
    verdict = EvaluatorVerdict(**verdict_dict)
    return route_after_evaluation(verdict)


async def retry_dispatch_node(state: OrchestratorState) -> OrchestratorState:
    db = state.get("db")
    trip_id = state.get("trip_id")
    verdict_dict = state.get("evaluator_verdict") or {}
    retry_count = state.get("evaluator_retry_count", 0) + 1

    failures = verdict_dict.get("failures", [])
    agent_to_retry = (
        next_agent_for_failures([EvaluatorFailure(**f) for f in failures])
        if failures else "activities_agent"
    )

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

    Phase 13: logs a persist agent_runs row so the write step is visible
    in the timeline alongside all agent decisions.
    """
    db = state.get("db")
    trip_id = state.get("trip_id")
    draft = state.get("draft_itinerary") or {}

    if db is None or trip_id is None:
        return {**state, "itinerary_id": None}

    async with timed_run() as timer:
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

    await log_agent_run(
        db=db,
        trip_id=trip_id,
        agent_name="persist",
        input={
            "total_cost": draft.get("total_cost"),
            "days_count": len(draft.get("days", [])),
            "currency": draft.get("currency", "INR"),
        },
        output={
            "itinerary_id": str(itinerary.id),
            "trip_status": "completed",
        },
        duration_ms=timer.duration_ms,
        status="completed",
    )

    try:
        await generate_embeddings(itinerary.id)
    except Exception:
        pass

    return {**state, "itinerary_id": itinerary.id}


async def builder_failed_node(state: OrchestratorState) -> OrchestratorState:
    """Evaluator/Builder retries exhausted — mark the trip failed honestly.

    Phase 13: logs a builder_failed agent_runs row so the failure reason
    is queryable alongside the retry chain.
    """
    db = state.get("db")
    trip_id = state.get("trip_id")
    publish_fn = state.get("publish_fn")
    builder_error = state.get("builder_error") or {}
    retry_count = state.get("evaluator_retry_count", 0)

    async with timed_run() as timer:
        if db is not None and trip_id is not None:
            trip = await db.get(Trip, trip_id)
            if trip is not None:
                trip.status = TripStatus.FAILED
                db.add(trip)
                await db.commit()

    if db is not None and trip_id is not None:
        await log_agent_run(
            db=db,
            trip_id=trip_id,
            agent_name="builder_failed",
            input={
                "evaluator_retry_count": retry_count,
                "builder_error_code": builder_error.get("code"),
            },
            output={
                "builder_error": builder_error,
                "trip_status": "failed",
            },
            duration_ms=timer.duration_ms,
            status="failed",
        )

    if publish_fn:
        try:
            error_detail = builder_error.get("error", "Itinerary could not be built after maximum retries.")
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
    """Publish planning_complete. Not logged — pure publish, no decision."""
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
        {"continue": "hotel_activities", "replan": "run_flight", "escalate": "escalate"},
    )
    graph.add_edge("hotel_activities", "build_itinerary")
    graph.add_edge("build_itinerary", "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        route_after_evaluator,
        {"passed": "persist", "retry": "retry_dispatch", "failed": "builder_failed"},
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
            **input_state,
            "db": db if db is not None else input_state.get("db"),
            "trip_id": trip_id if trip_id is not None else input_state.get("trip_id"),
            "publish_fn": publish_fn if publish_fn is not None else input_state.get("publish_fn"),
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

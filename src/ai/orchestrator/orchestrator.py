"""OrchestratorAgent — the planning graph (Phases 9–16).

    intent_parsing → apply_preferences → run_flight → budget_decision
        ├─ continue → hotel_activities → build_itinerary → evaluate
        │                 ├─ passed → persist → merge → extract_preferences → END
        │                 ├─ retry  → retry_dispatch → build_itinerary
        │                 └─ failed → builder_failed → END
        ├─ replan   → run_flight   (≤ MAX_REPLAN_ATTEMPTS; tighter cap, one more stop)
        └─ escalate → escalate → END

Every decision node writes an agent_runs row tagged with the conversation
`turn` (Phases 13, 15). Preferences are injected once, into state, right after
intent parsing (Phase 16) so every later call site inherits them.

refine() (Phase 15) handles turn 2+. full_replan / add_day re-enter the graph
with a clean slate; targeted refinements re-run only the implicated sub-agent,
carry everything else forward, and go straight to build → evaluate → persist.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, timedelta
from typing import Any

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypedDict

from src.ai.agents.activities_agent import ActivitiesAgent
from src.ai.agents.budget_decision import make_budget_decision, replan_flight_budget, viable_budget
from src.ai.agents.evaluator import (
    MAX_EVALUATOR_RETRIES,
    EvaluatorAgent,
    EvaluatorFailure,
    EvaluatorVerdict,
    expected_total_cost,
    next_agent_for_failures,
    route_after_evaluation,
)
from src.ai.agents.flight_agent import FlightAgent
from src.ai.agents.hotel_agent import HotelAgent
from src.ai.agents.preference_extractor import PreferenceExtractor
from src.ai.agents.refinement_classifier import RefinementType
from src.ai.builder.builder import ItineraryBuilder
from src.ai.llm import GEMINI_MODEL, parse_json_object
from src.ai.utils.embeddings import generate_embeddings
from src.ai.utils.preferences import (
    build_preference_updates,
    get_trip_user_id,
    load_preferences,
    preferences_to_dict,
    preferred_airlines_from,
)
from src.ai.utils.run_logger import log_agent_run, timed_run
from src.ai.utils.tasks import spawn

try:
    from app.models.itinerary import Itinerary
    from app.models.trip import Trip, TripStatus
except ImportError:
    from src.backend.app.models.itinerary import Itinerary
    from src.backend.app.models.trip import Trip, TripStatus

logger = logging.getLogger(__name__)

TRIP_FIELDS = ("destination", "origin", "start_date", "end_date", "budget", "group_size", "interests")
_RUNTIME_KEYS = ("db", "trip_id", "publish_fn", "on_complete")  # live objects — never logged, saved or returned

# "Ask vs. assume" defaults (prompts/orchestrator_v3.md): these are assumed, never asked for.
DEFAULT_ORIGIN = "DEL"
DEFAULT_INTERESTS = ["sightseeing"]
MAX_FLIGHT_STOPS = 2
ATTRACTIONS_LIMIT = 10  # the tool's maximum — enough variety for a week without repeats


# ── State ─────────────────────────────────────────────────────────────────


class OrchestratorState(TypedDict, total=False):
    raw_input: str | None
    intent_override: bool  # full_replan: fields parsed from raw_input replace existing ones
    refinement_request: str | None  # targeted refinement: the user's message, as builder context

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

    # Phase 15: conversation turn (1-indexed). Forwarded to every log_agent_run call.
    turn: int

    # Phase 16: snapshot of the user's saved preferences (set by apply_preferences_node)
    preferences: dict | None

    # Runtime helpers — never stored in DB
    publish_fn: Any | None
    on_complete: Any | None  # async callable(state), awaited just before planning_complete is published
    db: Any | None
    trip_id: Any | None


def _public(state: dict) -> dict:
    return {k: v for k, v in state.items() if k not in _RUNTIME_KEYS}


async def _publish(state: OrchestratorState, event: dict) -> None:
    """Send one SSE event. A dead Redis must never fail a planning run."""
    publish_fn = state.get("publish_fn")
    if publish_fn is None:
        return
    try:
        await publish_fn(event)
    except Exception:
        logger.warning("SSE publish failed for %s", event.get("event") or event.get("agent"), exc_info=True)


async def _log(
    state: OrchestratorState, agent_name: str, input: dict, output: dict, duration_ms: int, status: str = "completed"
) -> None:
    """Write an agent_runs row for a graph node (no-op without a DB session)."""
    db, trip_id = state.get("db"), state.get("trip_id")
    if db is not None and trip_id is not None:
        await log_agent_run(
            db=db,
            trip_id=trip_id,
            agent_name=agent_name,
            input=input,
            output=output,
            duration_ms=duration_ms,
            status=status,
            turn=state.get("turn", 1),
        )


async def _set_trip_status(state: OrchestratorState, status: TripStatus) -> None:
    db, trip_id = state.get("db"), state.get("trip_id")
    if db is None or trip_id is None:
        return
    trip = await db.get(Trip, trip_id)
    if trip is not None:
        trip.status = status
        _sync_trip(trip, state)  # keep what intent parsing corrected (e.g. the destination) even if the run failed
        db.add(trip)
        await db.commit()


# ── Intent parsing ─────────────────────────────────────────────────────────

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
  If any activity intent is expressed at all (even \"sightseeing\" or \"normal tourism\"), never leave it null.
- origin: if not mentioned, use null (caller defaults to \"DEL\").
- destination absent → set null, do NOT guess.
- Return ONLY the JSON object. No explanation, no markdown fences.

User message: {message}"""


def _iso_date(value: Any) -> str | None:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def _shift_date(value: str | None, days: int) -> str | None:
    parsed = _iso_date(value)
    return (date.fromisoformat(parsed) + timedelta(days=days)).isoformat() if parsed else value


def _clean_intent(parsed: dict) -> dict:
    """Keep only well-typed fields from the LLM's reply — it is never trusted as-is."""
    clean: dict[str, Any] = {}
    for key in ("destination", "origin"):
        if isinstance(parsed.get(key), str) and parsed[key].strip():
            clean[key] = parsed[key].strip()
    for key in ("start_date", "end_date"):
        if _iso_date(parsed.get(key)):
            clean[key] = _iso_date(parsed[key])
    if (
        isinstance(parsed.get("budget"), (int, float))
        and not isinstance(parsed["budget"], bool)
        and parsed["budget"] > 0
    ):
        clean["budget"] = float(parsed["budget"])
    if (
        isinstance(parsed.get("group_size"), int)
        and not isinstance(parsed["group_size"], bool)
        and parsed["group_size"] >= 1
    ):
        clean["group_size"] = parsed["group_size"]
    if isinstance(parsed.get("interests"), list):
        interests = [i.strip() for i in parsed["interests"] if isinstance(i, str) and i.strip()]
        if interests:
            clean["interests"] = interests
    return clean


async def _extract_intent(message: str) -> dict:
    """One Gemini call: free text → validated trip fields. Raises on LLM failure."""
    llm = ChatGoogleGenerativeAI(model=GEMINI_MODEL, temperature=0)
    prompt = _INTENT_PROMPT.format(today=date.today().isoformat(), message=message)
    return _clean_intent(parse_json_object((await llm.ainvoke(prompt)).content))


# ── Sub-agent runners ──────────────────────────────────────────────────────


def _nights(state: OrchestratorState) -> int:
    try:
        return max(1, (date.fromisoformat(state["end_date"]) - date.fromisoformat(state["start_date"])).days)
    except (KeyError, TypeError, ValueError):
        return 7


async def _run_agent(
    state: OrchestratorState, agent: Any, name: str, input_state: dict, result_key: str, noun: str
) -> tuple[list[dict], dict | None]:
    """Run one sub-agent and publish its SSE status. Returns (items, error) — never raises."""
    try:
        result = await agent.run(
            input_state, db=state.get("db"), trip_id=state.get("trip_id"), turn=state.get("turn", 1)
        )
        items, error = result.get(result_key) or [], result.get("error")
        if not error and result.get("clarification_question"):
            # Standalone, the agent would ask the user; inside the orchestrator that is a failed search.
            error = {"error": result["clarification_question"], "code": "MISSING_INPUT"}
    except Exception as exc:
        logger.exception("%s crashed", name)
        items, error = [], {"error": str(exc), "code": "AGENT_EXCEPTION"}

    await _publish(
        state,
        {
            "agent": name,
            "status": "failed" if error else "completed",
            "summary": error.get("error", "Failed") if error else f"Found {len(items)} {noun}",
        },
    )
    return ([], error) if error else (items, None)


async def _search_flights(state: OrchestratorState, attempt: int = 0) -> dict:
    """Flight search → state updates. `attempt` > 0 is a re-plan: tighter budget cap, one more stop."""
    budget = state.get("budget") or 0.0
    flights, error = await _run_agent(
        state,
        FlightAgent(),
        "flight_agent",
        {
            "destination": state.get("destination", ""),
            "origin": state.get("origin") or DEFAULT_ORIGIN,
            "date": state.get("start_date", ""),
            "return_date": state.get("end_date") or None,
            "budget": replan_flight_budget(budget, attempt) if attempt else budget,
            "passengers": state.get("group_size") or 1,
            "preferred_airlines": preferred_airlines_from(state),
            "max_stops": min(MAX_FLIGHT_STOPS, 1 + attempt),
        },
        "flights",
        f"flights (re-plan attempt {attempt})" if attempt else "flights",
    )
    return {"flights": flights, "flight_error": error, "flight_status": "failed" if error else "completed"}


async def _search_hotels(state: OrchestratorState) -> dict:
    remaining = (state.get("budget_decision") or {}).get("remaining_budget")
    if remaining is None:
        remaining = state.get("budget") or 0.0
    hotels, error = await _run_agent(
        state,
        HotelAgent(),
        "hotel_agent",
        {
            "destination": state.get("destination", ""),
            "check_in": state.get("start_date", ""),
            "check_out": state.get("end_date", ""),
            "budget_per_night": round(remaining / _nights(state), 2),
            "guests": state.get("group_size") or 1,
        },
        "hotels",
        "hotels",
    )
    return {"hotels": hotels, "hotel_error": error, "hotel_status": "failed" if error else "completed"}


async def _search_activities(state: OrchestratorState) -> dict:
    attractions, error = await _run_agent(
        state,
        ActivitiesAgent(),
        "activities_agent",
        {
            "destination": state.get("destination", ""),
            "interests": state.get("interests") or DEFAULT_INTERESTS,
            "limit": ATTRACTIONS_LIMIT,
        },
        "attractions",
        "attractions",
    )
    return {
        "attractions": attractions,
        "activities_error": error,
        "activities_status": "failed" if error else "completed",
    }


# ── Nodes ─────────────────────────────────────────────────────────────────


def _is_free_text(destination: str | None) -> bool:
    """A destination field holding a sentence ("Relaxed trip from Delhi to Ayodhya") rather than a place name."""
    return bool(destination) and len(destination.split()) > 3


async def intent_parsing_node(state: OrchestratorState) -> OrchestratorState:
    """Fill trip fields from free text. Existing fields win unless `intent_override` is set.

    People also type a whole request into the trip's destination field; such a
    destination is parsed as free text too, and the place found in it replaces it.

    An LLM failure is logged and the run carries on with the fields it already has.
    """
    raw = state.get("raw_input")
    destination_text = state.get("destination") if _is_free_text(state.get("destination")) else None
    message = " ".join(filter(None, [destination_text, raw]))
    extracted: dict = {}
    status = "completed"

    async with timed_run() as timer:
        if message:
            try:
                parsed = await _extract_intent(message)
            except Exception:
                logger.warning("Intent parsing failed — continuing with existing trip fields", exc_info=True)
                parsed, status = {}, "failed"

            override = state.get("intent_override", False)
            extracted = {
                k: v
                for k, v in parsed.items()
                if override or not state.get(k) or (k == "destination" and destination_text)
            }

            # "Let's go in January instead" moves the start only — keep the trip the same length.
            if "start_date" in extracted and "end_date" not in extracted and _iso_date(state.get("start_date")):
                moved = (date.fromisoformat(extracted["start_date"]) - date.fromisoformat(state["start_date"])).days
                extracted["end_date"] = _shift_date(state.get("end_date"), moved)

    await _log(
        state,
        "intent_parsing",
        input={"raw_input": raw, "destination_text": destination_text},
        output={"extracted_fields": extracted, "pass_through": not message},
        duration_ms=timer.duration_ms,
        status=status,
    )
    return {**state, **extracted}


async def apply_preferences_node(state: OrchestratorState) -> OrchestratorState:
    """Phase 16 — load the user's saved preferences and inject them into the run.

    Runs AFTER intent parsing on purpose: intent parsing only fills `interests`
    when they are empty, so injecting dietary restrictions first would stop it
    extracting the user's real interests from raw_input.

    Injection happens once, in state, so every downstream call site (initial
    flight search, replan loop, evaluator retry, refine) picks it up for free.
    Never fails the run: any error is logged and the state passes through.
    """
    db = state.get("db")
    trip_id = state.get("trip_id")
    if db is None or trip_id is None:
        return state

    try:
        user_id = await get_trip_user_id(db, trip_id)
        if user_id is None:
            return state

        async with timed_run() as timer:
            prefs = await load_preferences(db, user_id)
            snapshot = preferences_to_dict(prefs)
            updates = build_preference_updates(state, snapshot) if prefs is not None else {}

        await _log(
            state,
            "preferences",
            input={"user_id": str(user_id)},
            output={
                "loaded": prefs is not None,
                "preferences": snapshot if prefs is not None else None,
                "state_updates": {k: v for k, v in updates.items() if k != "preferences"},
            },
            duration_ms=timer.duration_ms,
        )
        return {**state, **updates}
    except Exception:
        logger.exception("apply_preferences_node failed — continuing without preferences")
        return state


async def run_flight_node(state: OrchestratorState) -> OrchestratorState:
    return {**state, **await _search_flights(state, attempt=state.get("replan_attempts", 0))}


async def budget_decision_node(state: OrchestratorState) -> OrchestratorState:
    flights = state.get("flights", [])
    total_budget = state.get("budget") or 0.0
    replan_attempts = state.get("replan_attempts", 0)

    async with timed_run() as timer:
        decision = make_budget_decision(flights, total_budget, replan_attempts, state.get("flight_error"))

    budget_conflict_options: list[dict] | None = None
    if decision.decision == "escalate":
        # Offer a budget that actually clears the check, never less than +25%.
        target_budget = max(total_budget * 1.25, viable_budget(decision.flight_cost))
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
                "description": f"Increase total budget to ₹{target_budget:,.0f}",
                "estimated_saving": f"Additional ₹{target_budget - total_budget:,.0f}",
            },
        ]

    await _log(
        state,
        "budget_decision",
        input={
            "flights_evaluated": len(flights),
            "cheapest_flight": decision.flight_cost,
            "total_budget": total_budget,
            "replan_attempts": replan_attempts,
        },
        output=decision.model_dump(),
        duration_ms=timer.duration_ms,
    )

    return {
        **state,
        "budget_decision": decision.model_dump(),
        "budget_conflict_options": budget_conflict_options,
        "replan_attempts": replan_attempts + (1 if decision.decision == "replan" else 0),
    }


def route_after_budget_decision(state: OrchestratorState) -> str:
    return (state.get("budget_decision") or {}).get("decision", "continue")


async def hotel_activities_node(state: OrchestratorState) -> OrchestratorState:
    """Hotels and activities are independent of each other — search them concurrently."""
    hotel_updates, activities_updates = await asyncio.gather(_search_hotels(state), _search_activities(state))
    return {**state, **hotel_updates, **activities_updates}


async def escalate_node(state: OrchestratorState) -> OrchestratorState:
    """Budget conflict: fail the trip and hand the user the options (POST /trips/{id}/replan)."""
    bd = state.get("budget_decision") or {}
    options = state.get("budget_conflict_options") or []

    async with timed_run() as timer:
        await _set_trip_status(state, TripStatus.FAILED)

    await _log(
        state,
        "escalate",
        input={
            "flight_cost": bd.get("flight_cost", 0),
            "remaining_budget": bd.get("remaining_budget", 0),
            "total_budget": bd.get("total_budget", 0),
        },
        output={"reason": bd.get("reason", ""), "options_offered": len(options), "trip_status": "failed"},
        duration_ms=timer.duration_ms,
    )

    reason = bd.get("reason", "Flights exceed available budget.")
    await _publish(
        state,
        {
            "event": "budget_conflict",
            "reason": reason,
            "flight_cost": bd.get("flight_cost", 0),
            "remaining_budget": bd.get("remaining_budget", 0),
            "options": options,
        },
    )
    await _publish(state, {"event": "planning_failed", "agent": "orchestrator", "status": "failed", "error": reason})

    return {**state, "hotel_status": "skipped", "activities_status": "skipped"}


async def build_itinerary_node(state: OrchestratorState) -> OrchestratorState:
    trip_meta = {
        "destination": state.get("destination", ""),
        "start_date": state.get("start_date", ""),
        "end_date": state.get("end_date", ""),
        "group_size": state.get("group_size") or 1,
    }
    prefs = state.get("preferences")
    if prefs and any(prefs.values()):
        trip_meta["preferences"] = prefs
    if state.get("refinement_request"):
        trip_meta["request"] = state["refinement_request"]

    result = await ItineraryBuilder().run(
        trip_meta=trip_meta,
        flights=state.get("flights", []),
        hotels=state.get("hotels", []),
        attractions=state.get("attractions", []),
        db=state.get("db"),
        trip_id=state.get("trip_id"),
        turn=state.get("turn", 1),
    )

    return {**state, "draft_itinerary": result.get("draft"), "builder_error": result.get("error")}


async def evaluate_node(state: OrchestratorState) -> OrchestratorState:
    draft = state.get("draft_itinerary")
    retry_count = state.get("evaluator_retry_count", 0)

    if draft is None:
        return {**state, "evaluator_verdict": {"passed": False, "failures": [], "retry_count": retry_count}}

    verdict = await EvaluatorAgent().run(
        draft=draft,
        trip_start=state.get("start_date", ""),
        trip_end=state.get("end_date", ""),
        expected_budget_total=expected_total_cost(draft, state.get("flights", []), state.get("hotels", [])),
        attractions=state.get("attractions", []),
        retry_count=retry_count,
        db=state.get("db"),
        trip_id=state.get("trip_id"),
        turn=state.get("turn", 1),
    )
    return {**state, "evaluator_verdict": verdict.model_dump()}


def route_after_evaluator(state: OrchestratorState) -> str:
    if state.get("builder_error") is not None and state.get("draft_itinerary") is None:
        retry_count = state.get("evaluator_retry_count", 0)
        return "failed" if retry_count >= MAX_EVALUATOR_RETRIES else "retry"

    verdict_dict = state.get("evaluator_verdict") or {"passed": False, "failures": [], "retry_count": 0}
    return route_after_evaluation(EvaluatorVerdict(**verdict_dict))


async def retry_dispatch_node(state: OrchestratorState) -> OrchestratorState:
    """Re-run only the sub-agent the evaluator's failures point at, then loop back to the builder."""
    failures = (state.get("evaluator_verdict") or {}).get("failures", [])
    agent_to_retry = next_agent_for_failures([EvaluatorFailure(**f) for f in failures]) or "activities_agent"

    if agent_to_retry == "flight_agent":
        key, updates = "flights", await _search_flights(state)
    else:
        key, updates = "attractions", await _search_activities(state)

    new_state: OrchestratorState = {**state, "evaluator_retry_count": state.get("evaluator_retry_count", 0) + 1}
    if updates[key]:  # a failed retry keeps the previous results rather than wiping them
        new_state.update(updates)
    return new_state


def _sync_trip(trip: Any, state: OrchestratorState) -> None:
    """Keep the trip row in step with what was actually planned (a refinement may move dates or destination)."""
    for field in ("destination", "budget", "group_size"):
        if state.get(field):
            setattr(trip, field, state[field])
    for field in ("start_date", "end_date"):
        if _iso_date(state.get(field)):
            setattr(trip, field, date.fromisoformat(state[field]))


async def persist_node(state: OrchestratorState) -> OrchestratorState:
    """Write the itinerary row + mark the trip completed — one commit, atomic.

    Phase 15: always creates a NEW itinerary row (INSERT, never UPDATE)
    so every turn's output is preserved in history. The latest row by
    created_at is what GET /trips/{id}/itinerary returns; all versions are
    accessible via GET /trips/{id}/itineraries.
    """
    db = state.get("db")
    trip_id = state.get("trip_id")
    draft = state.get("draft_itinerary") or {}

    if db is None or trip_id is None:
        return {**state, "itinerary_id": None}

    async with timed_run() as timer:
        itinerary = Itinerary(trip_id=trip_id, structured_data=draft, total_cost=draft.get("total_cost"))
        trip = await db.get(Trip, trip_id)
        if trip is not None:
            trip.status = TripStatus.COMPLETED
            _sync_trip(trip, state)
            db.add(trip)
        db.add(itinerary)
        await db.commit()
        await db.refresh(itinerary)

    await _log(
        state,
        "persist",
        input={
            "total_cost": draft.get("total_cost"),
            "days_count": len(draft.get("days", [])),
            "currency": draft.get("currency", "INR"),
        },
        output={"itinerary_id": str(itinerary.id), "trip_status": "completed"},
        duration_ms=timer.duration_ms,
    )

    # Phase 14: embeddings run in the background (own DB session, never raises) so
    # the embedding call and its retries cannot delay `planning_complete`.
    spawn(generate_embeddings(itinerary.id))

    return {**state, "itinerary_id": itinerary.id}


async def builder_failed_node(state: OrchestratorState) -> OrchestratorState:
    """The build/evaluate loop ran out of retries — fail the trip with an honest reason."""
    builder_error = state.get("builder_error") or {}
    failures = (state.get("evaluator_verdict") or {}).get("failures", [])
    reason = (
        builder_error.get("error")
        or "; ".join(f["detail"] for f in failures)
        or "Itinerary could not be built after maximum retries."
    )

    async with timed_run() as timer:
        await _set_trip_status(state, TripStatus.FAILED)

    await _log(
        state,
        "builder_failed",
        input={
            "evaluator_retry_count": state.get("evaluator_retry_count", 0),
            "builder_error_code": builder_error.get("code"),
        },
        output={"builder_error": builder_error, "evaluator_failures": failures, "trip_status": "failed"},
        duration_ms=timer.duration_ms,
        status="failed",
    )

    await _publish(
        state, {"event": "planning_failed", "agent": "itinerary_builder", "status": "failed", "error": reason}
    )
    return state


async def merge_node(state: OrchestratorState) -> OrchestratorState:
    """Publish planning_complete. Not logged — pure publish, no decision.

    `on_complete` runs first: the caller saves whatever a follow-up request
    needs (the refinement state) BEFORE clients are told they may send one.
    """
    on_complete = state.get("on_complete")
    if on_complete is not None and state.get("itinerary_id"):
        try:
            await on_complete(_public(state))
        except Exception:
            logger.warning("on_complete hook failed", exc_info=True)

    statuses = (state.get("flight_status"), state.get("hotel_status"), state.get("activities_status"))
    await _publish(
        state,
        {
            "event": "planning_complete",
            "agents_done": sum(1 for s in statuses if s == "completed"),
            "agents_total": 3,
            "itinerary_id": str(state["itinerary_id"]) if state.get("itinerary_id") else None,
            "status": "completed" if state.get("itinerary_id") else "failed",
            "turn": state.get("turn", 1),
        },
    )
    return state


async def extract_preferences_node(state: OrchestratorState) -> OrchestratorState:
    """Phase 16 — learn lasting preferences from the finished trip (additive).

    Placed after merge_node so `planning_complete` is published before the
    extractor's LLM call adds latency. Failures never affect the trip.
    """
    db = state.get("db")
    trip_id = state.get("trip_id")
    if db is None or trip_id is None or not state.get("draft_itinerary"):
        return state

    try:
        user_id = await get_trip_user_id(db, trip_id)
        if user_id is not None:
            await PreferenceExtractor().run(state, db=db, user_id=user_id, trip_id=trip_id, turn=state.get("turn", 1))
    except Exception:
        logger.exception("PreferenceExtractor failed — planning result unaffected")
        try:
            await db.rollback()  # keep the shared session usable for later log rows
        except Exception:
            pass
    return state


# ── Graph ─────────────────────────────────────────────────────────────────


def build_orchestrator_graph():
    graph = StateGraph(OrchestratorState)

    graph.add_node("intent_parsing", intent_parsing_node)
    graph.add_node("apply_preferences", apply_preferences_node)
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
    graph.add_node("extract_preferences", extract_preferences_node)

    graph.set_entry_point("intent_parsing")
    graph.add_edge("intent_parsing", "apply_preferences")
    graph.add_edge("apply_preferences", "run_flight")
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
    graph.add_edge("merge", "extract_preferences")
    graph.add_edge("extract_preferences", END)
    graph.add_edge("builder_failed", END)
    graph.add_edge("escalate", END)

    return graph.compile()


# ── Agent wrapper ─────────────────────────────────────────────────────────

_RESULT_DEFAULTS: dict[str, Any] = {
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
# What a targeted refinement must start afresh; everything else is carried forward.
_BUILD_RESET = {
    k: _RESULT_DEFAULTS[k]
    for k in ("draft_itinerary", "builder_error", "evaluator_verdict", "evaluator_retry_count", "itinerary_id")
}


def _result_summary(state: dict) -> dict:
    return {
        "flights_count": len(state.get("flights", [])),
        "hotels_count": len(state.get("hotels", [])),
        "attractions_count": len(state.get("attractions", [])),
        "flight_status": state.get("flight_status"),
        "hotel_status": state.get("hotel_status"),
        "activities_status": state.get("activities_status"),
        "budget_decision": state.get("budget_decision"),
        "replan_attempts": state.get("replan_attempts", 0),
        "evaluator_retry_count": state.get("evaluator_retry_count", 0),
        "itinerary_id": str(state["itinerary_id"]) if state.get("itinerary_id") else None,
    }


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
        turn: int = 1,
        on_complete=None,
    ) -> dict:
        full_state = {
            **_RESULT_DEFAULTS,
            **input_state,
            "turn": turn,
            "db": db if db is not None else input_state.get("db"),
            "trip_id": trip_id if trip_id is not None else input_state.get("trip_id"),
            "publish_fn": publish_fn if publish_fn is not None else input_state.get("publish_fn"),
            "on_complete": on_complete,
        }

        async with timed_run() as timer:
            result = await self._graph.ainvoke(full_state)

        await _log(
            full_state,
            "orchestrator",
            input=_public(input_state),
            output=_result_summary(result),
            duration_ms=timer.duration_ms,
            status="completed" if result.get("itinerary_id") else "failed",
        )
        return _public(result)

    async def refine(
        self,
        refinement_type: RefinementType,
        prior_state: dict,
        refinement_message: str,
        db: AsyncSession | None = None,
        trip_id: uuid.UUID | None = None,
        publish_fn=None,
        turn: int = 2,
        on_complete=None,
    ) -> dict:
        """Turn 2+: apply one refinement to an already-planned trip.

        full_replan / add_day → the full graph on a clean slate (only the trip
        fields survive; full_replan lets the new message override them).

        targeted_* → re-run just that sub-agent, carry the other results
        forward unchanged, then build → evaluate → persist. The user's message
        goes to the builder as context so it can choose differently among the
        provided data ("closer to the beach", "a direct flight").
        """
        if refinement_type in ("full_replan", "add_day"):
            fresh: dict = {k: prior_state.get(k) for k in TRIP_FIELDS}
            if refinement_type == "full_replan":
                fresh.update(raw_input=refinement_message, intent_override=True)
            else:
                fresh["end_date"] = _shift_date(fresh.get("end_date"), 1)
            return await self.run(
                fresh, db=db, trip_id=trip_id, publish_fn=publish_fn, turn=turn, on_complete=on_complete
            )

        state: OrchestratorState = {
            **_RESULT_DEFAULTS,
            **prior_state,
            **_BUILD_RESET,
            "refinement_request": refinement_message,
            "turn": turn,
            "db": db,
            "trip_id": trip_id,
            "publish_fn": publish_fn,
            "on_complete": on_complete,
        }

        async with timed_run() as timer:
            escalated = False
            if refinement_type == "targeted_flights":
                updates = await _search_flights(state)
                if updates["flights"]:  # a failed search keeps the previous flights
                    # "Make it cheaper" → new flights, then the budget check decides whether the plan still stands.
                    state = await budget_decision_node({**state, **updates, "replan_attempts": 0})
                    escalated = route_after_budget_decision(state) == "escalate"
            elif refinement_type == "targeted_hotel":
                updates = await _search_hotels(state)
                if updates["hotels"]:
                    state.update(updates)
            else:  # targeted_activities — the message usually names the new interests
                try:
                    new_interests = (await _extract_intent(refinement_message)).get("interests")
                except Exception:
                    new_interests = None
                if new_interests:
                    state["interests"] = new_interests
                updates = await _search_activities(state)
                if updates["attractions"]:
                    state.update(updates)

            if escalated:
                state = await escalate_node(state)
            else:
                state = await evaluate_node(await build_itinerary_node(state))
                while route_after_evaluator(state) == "retry":  # bounded by MAX_EVALUATOR_RETRIES
                    state = await evaluate_node(await build_itinerary_node(await retry_dispatch_node(state)))

                if route_after_evaluator(state) == "passed":
                    state = await merge_node(await persist_node(state))
                else:
                    state = await builder_failed_node(state)

        await _log(
            state,
            "orchestrator",
            input={"refinement_type": refinement_type, "message": refinement_message},
            output=_result_summary(state),
            duration_ms=timer.duration_ms,
            status="completed" if state.get("itinerary_id") else "failed",
        )
        return _public(state)

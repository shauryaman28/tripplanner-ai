"""OrchestratorAgent — the planning graph (Phases 9–22).

    intent_parsing → apply_preferences → run_flight → budget_decision
        ├─ continue → hotel_activities (+ local tips) → build_itinerary → evaluate
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

While the builder writes, its reply is published piece by piece as
`builder_token` events (Phase 20) — see _TokenStream.

The DestinationIntelligenceAgent (Phase 22) runs beside the hotel and
activities searches. It is the one agent a plan does not depend on: it
publishes nothing, and when it has nothing to say the plan is made without
local tips — see _local_tips.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import date, timedelta
from typing import Any

from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypedDict

from src.ai.agents.activities_agent import ActivitiesAgent
from src.ai.agents.budget_alternatives import conflict_alternatives, plain_options
from src.ai.agents.budget_decision import BudgetDecision, make_budget_decision, replan_flight_budget
from src.ai.agents.destination_intelligence import UNKNOWN_DESTINATION, DestinationIntelligenceAgent
from src.ai.agents.evaluator import (
    MAX_EVALUATOR_RETRIES,
    EvaluatorAgent,
    EvaluatorFailure,
    EvaluatorVerdict,
    itinerary_estimate,
    next_agent_for_failures,
    route_after_evaluation,
)
from src.ai.agents.flight_agent import FlightAgent
from src.ai.agents.hotel_agent import HotelAgent
from src.ai.agents.preference_extractor import PreferenceExtractor
from src.ai.agents.refinement_classifier import RefinementType
from src.ai.builder.builder import ItineraryBuilder
from src.ai.itinerary import FREE_TIME, SLOTS
from src.ai.llm import ask, parse_json_object
from src.ai.utils.embeddings import generate_embeddings
from src.ai.utils.failures import can_retry, failure_words
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
    previous_plan: list[dict] | None  # targeted refinement: the plan being changed, so the rest of it is kept

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
    # Phase 21: the trip as asked, priced, when the budget check stops; and the way out the traveller
    # picked (POST /replan), which the check then goes ahead with instead of asking again
    budget_estimate: dict | None
    budget_choice: dict | None

    # Phase 22: what the DestinationIntelligenceAgent knew about the place (None: nothing — and why)
    local_intelligence: dict | None
    intelligence_error: dict | None

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
    """One LLM call: free text → validated trip fields. Raises on LLM failure."""
    prompt = _INTENT_PROMPT.format(today=date.today().isoformat(), message=message)
    return _clean_intent(parse_json_object(await ask(prompt)))


# ── Sub-agent runners ──────────────────────────────────────────────────────


def _nights(state: OrchestratorState) -> int:
    try:
        return max(1, (date.fromisoformat(state["end_date"]) - date.fromisoformat(state["start_date"])).days)
    except (KeyError, TypeError, ValueError):
        return 7


def _travel_month(state: OrchestratorState) -> int | None:
    """The month the trip starts in — the season its prices belong to (Phase 21)."""
    try:
        return date.fromisoformat(state["start_date"]).month
    except (KeyError, TypeError, ValueError):
        return None


async def _run_agent(
    state: OrchestratorState, agent: Any, name: str, input_state: dict, result_key: str, noun: str, note: str = ""
) -> tuple[list[dict], dict | None]:
    """Run one sub-agent and publish its SSE status. Returns (items, error) — never raises.

    `noun` (plural) and `note` word the summary: "Found 3 flights (re-plan attempt 1)".
    """
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

    found = f"Found {len(items)} {noun if len(items) != 1 else noun.removesuffix('s')}{note}"
    if error:
        # In the traveller's words, and whether running it again could help (Phase 20: the page offers a Retry)
        update = {"agent": name, "status": "failed", "summary": failure_words(error), "retryable": can_retry(error)}
    else:
        update = {"agent": name, "status": "completed", "summary": found}
    await _publish(state, update)
    return ([], error) if error else (items, None)


# What each search agent is given. Functions of the state alone, so that cache warming (warming.py,
# Phase 24) can make the very searches a plan will make, before the plan is asked for.


def flight_search_input(state: OrchestratorState, attempt: int = 0) -> dict:
    """The flight agent's input. `attempt` > 0 is a re-plan: tighter budget cap, one more stop."""
    budget = state.get("budget") or 0.0
    return {
        "destination": state.get("destination", ""),
        "origin": state.get("origin") or DEFAULT_ORIGIN,
        "date": state.get("start_date", ""),
        "return_date": state.get("end_date") or None,
        "budget": replan_flight_budget(budget, attempt) if attempt else budget,
        "passengers": state.get("group_size") or 1,
        "preferred_airlines": preferred_airlines_from(state),
        "max_stops": min(MAX_FLIGHT_STOPS, 1 + attempt),
    }


def hotel_search_input(state: OrchestratorState) -> dict:
    """The hotel agent's input: what is left of the budget after the flights, a night."""
    remaining = (state.get("budget_decision") or {}).get("remaining_budget")
    if remaining is None:
        remaining = state.get("budget") or 0.0
    return {
        "destination": state.get("destination", ""),
        "check_in": state.get("start_date", ""),
        "check_out": state.get("end_date", ""),
        "budget_per_night": round(remaining / _nights(state), 2),
        "guests": state.get("group_size") or 1,
    }


def activities_search_input(state: OrchestratorState) -> dict:
    """The activities agent's input."""
    return {
        "destination": state.get("destination", ""),
        "interests": state.get("interests") or DEFAULT_INTERESTS,
        "limit": ATTRACTIONS_LIMIT,
    }


async def _search_flights(state: OrchestratorState, attempt: int = 0) -> dict:
    """Flight search → state updates. `attempt` > 0 is a re-plan: tighter budget cap, one more stop."""
    flights, error = await _run_agent(
        state,
        FlightAgent(),
        "flight_agent",
        flight_search_input(state, attempt),
        "flights",
        "flights",
        note=f" (re-plan attempt {attempt})" if attempt else "",
    )
    return {"flights": flights, "flight_error": error, "flight_status": "failed" if error else "completed"}


async def _search_hotels(state: OrchestratorState) -> dict:
    hotels, error = await _run_agent(
        state,
        HotelAgent(),
        "hotel_agent",
        hotel_search_input(state),
        "hotels",
        "hotels",
    )
    return {"hotels": hotels, "hotel_error": error, "hotel_status": "failed" if error else "completed"}


async def _search_activities(state: OrchestratorState) -> dict:
    attractions, error = await _run_agent(
        state,
        ActivitiesAgent(),
        "activities_agent",
        activities_search_input(state),
        "attractions",
        "attractions",
    )
    return {
        "attractions": attractions,
        "activities_error": error,
        "activities_status": "failed" if error else "completed",
    }


def _wants_local_tips(state: OrchestratorState) -> bool:
    """No tips yet — and not because the model said it does not know the place (asking again would not help)."""
    if state.get("local_intelligence"):
        return False
    return (state.get("intelligence_error") or {}).get("code") != UNKNOWN_DESTINATION


async def _local_tips(state: OrchestratorState) -> dict:
    """Destination intelligence → state updates (Phase 22).

    Unlike a search it never fails a plan and publishes nothing: without tips
    the plan is simply made without them. Tips a plan already has are kept —
    a change to the hotel does not change what is true of the place.
    """
    if not _wants_local_tips(state):
        return {}
    try:
        result = await DestinationIntelligenceAgent().run(
            {
                "destination": state.get("destination", ""),
                "start_date": state.get("start_date"),
                "end_date": state.get("end_date"),
                "group_size": state.get("group_size") or 1,
                "interests": state.get("interests") or DEFAULT_INTERESTS,
            },
            db=state.get("db"),
            trip_id=state.get("trip_id"),
            turn=state.get("turn", 1),
        )
    except Exception as exc:
        logger.exception("DestinationIntelligenceAgent crashed — planning without local tips")
        return {"local_intelligence": None, "intelligence_error": {"error": str(exc), "code": "AGENT_EXCEPTION"}}
    return {"local_intelligence": result.get("local_intelligence"), "intelligence_error": result.get("error")}


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


# The ways out that leave the flights as they are: once the traveller has picked one, the budget check
# goes ahead with it instead of stopping them again on the same flights (Phase 21).
GOING_AHEAD = {"reduce_days": "the shorter trip", "cheaper_hotel": "the cheaper stay"}


def _trip_dates(state: OrchestratorState) -> tuple[date, date] | None:
    try:
        return date.fromisoformat(state["start_date"]), date.fromisoformat(state["end_date"])
    except (KeyError, TypeError, ValueError):
        return None


def _settle_conflict(
    state: OrchestratorState, decision: BudgetDecision
) -> tuple[BudgetDecision, dict | None, list[dict] | None]:
    """Phase 21 — before stopping a trip on its flights, price the rest of it.

    Returns the decision (possibly "continue" after all), the trip as asked
    with its price and range, and the ways out with theirs (budget_alternatives.py).
    Without a fare or dates there is nothing to price: the two plain options.
    """
    budget, fare = decision.total_budget, decision.flight_cost
    replan_attempts = state.get("replan_attempts", 0)
    dates = _trip_dates(state)
    if fare <= 0 or dates is None:
        return decision, None, plain_options(fare, budget, replan_attempts)

    share = f"{fare / budget:.0%} of the budget" if budget else "the whole budget"
    choice = (state.get("budget_choice") or {}).get("choice")
    if choice in GOING_AHEAD and decision.remaining_budget > 0:
        reason = f"Flights cost ₹{fare:,.0f} — {share} — going ahead with {GOING_AHEAD[choice]} you chose."
        return decision.model_copy(update={"decision": "continue", "reason": reason}), None, None

    start, end = dates
    trip, options = conflict_alternatives(
        flight_cost=fare,
        budget=budget,
        start=start,
        end=end,
        travellers=state.get("group_size") or 1,
        destination=state.get("destination") or "",
        replan_attempts=replan_attempts,
        today=date.today(),
    )
    if trip["total"] <= budget:
        # The share rule is blunt: a short trip can spend most of its budget on flights and still fit.
        reason = (
            f"Flights cost ₹{fare:,.0f} — {share} — but at typical prices the rest still fits: "
            f"{trip['stay']} and things to do bring the trip to about ₹{trip['total']:,} of ₹{budget:,.0f}."
        )
        return decision.model_copy(update={"decision": "continue", "reason": reason}), None, None
    return decision, trip, options


async def budget_decision_node(state: OrchestratorState) -> OrchestratorState:
    flights = state.get("flights", [])
    total_budget = state.get("budget") or 0.0
    replan_attempts = state.get("replan_attempts", 0)

    async with timed_run() as timer:
        decision = make_budget_decision(flights, total_budget, replan_attempts, state.get("flight_error"))
        budget_estimate: dict | None = None
        budget_conflict_options: list[dict] | None = None
        if decision.decision == "escalate":
            decision, budget_estimate, budget_conflict_options = _settle_conflict(state, decision)

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
        "budget_estimate": budget_estimate,
        "replan_attempts": replan_attempts + (1 if decision.decision == "replan" else 0),
    }


def route_after_budget_decision(state: OrchestratorState) -> str:
    return (state.get("budget_decision") or {}).get("decision", "continue")


async def hotel_activities_node(state: OrchestratorState) -> OrchestratorState:
    """Hotels and activities are independent of each other — search them concurrently.

    Phase 22: the DestinationIntelligenceAgent runs beside them. It runs here,
    not beside the flight search, because a trip the budget check stops has no
    use for local tips (DECISIONS #133).
    """
    hotel_updates, activities_updates, tips = await asyncio.gather(
        _search_hotels(state), _search_activities(state), _local_tips(state)
    )
    return {**state, **hotel_updates, **activities_updates, **tips}


async def escalate_node(state: OrchestratorState) -> OrchestratorState:
    """Budget conflict: fail the trip and hand the user the options (POST /trips/{id}/replan)."""
    bd = state.get("budget_decision") or {}
    options = state.get("budget_conflict_options") or []
    reason = bd.get("reason") or "Flights exceed the available budget."

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
        # the options are stored, not just counted: GET /status hands them back after a page reload
        output={
            "reason": reason,
            "options": options,
            "estimate": state.get("budget_estimate"),
            "trip_status": "failed",
        },
        duration_ms=timer.duration_ms,
    )

    await _publish(
        state,
        {
            "event": "budget_conflict",
            "reason": reason,
            "flight_cost": bd.get("flight_cost", 0),
            "remaining_budget": bd.get("remaining_budget", 0),
            "options": options,
            # Phase 21: the trip as asked, priced — what the options' savings are measured from
            "estimate": state.get("budget_estimate"),
        },
    )
    await _publish(state, {"event": "planning_failed", "agent": "orchestrator", "status": "failed", "error": reason})

    return {**state, "hotel_status": "skipped", "activities_status": "skipped"}


# The model writes several hundred tokens a second and every publish is a round trip to Redis,
# so pieces written within this many seconds of each other travel in one event.
TOKEN_FLUSH_SECONDS = 0.05


class _TokenStream:
    """Publishes the builder's reply while it is being written (Phase 20).

    Events: {"event": "builder_token", "agent": "itinerary_builder", "token": <text>, "seq": <n>}.
    `seq` counts the events of one build from 0. The text only makes sense read
    from its start, so a client that joins late — or sees a gap — waits for the
    next seq 0 (a retry of the build starts a new stream).

    What is streamed is the model's unchecked reply. It is shown as a preview;
    the itinerary that is saved is the one that passed the builder's and the
    evaluator's checks afterwards.
    """

    def __init__(self, state: OrchestratorState) -> None:
        self._state = state
        self._pieces: list[str] = []
        self._seq = 0
        self._flushed_at = 0.0

    async def push(self, piece: str) -> None:
        self._pieces.append(piece)
        if self._seq == 0 or time.monotonic() - self._flushed_at >= TOKEN_FLUSH_SECONDS:
            await self.flush()  # the first piece goes out at once: it is what tells the page writing has begun

    async def flush(self) -> None:
        if not self._pieces:
            return
        token, self._pieces = "".join(self._pieces), []
        await _publish(
            self._state,
            {"event": "builder_token", "agent": "itinerary_builder", "token": token, "seq": self._seq},
        )
        self._seq += 1
        self._flushed_at = time.monotonic()


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
        if state.get("previous_plan"):
            trip_meta["previous_plan"] = state["previous_plan"]

    stream = _TokenStream(state) if state.get("publish_fn") is not None else None  # nobody listening → no streaming
    result = await ItineraryBuilder().run(
        trip_meta=trip_meta,
        flights=state.get("flights", []),
        hotels=state.get("hotels", []),
        attractions=state.get("attractions", []),
        db=state.get("db"),
        trip_id=state.get("trip_id"),
        turn=state.get("turn", 1),
        on_token=stream.push if stream else None,
        local_intelligence=state.get("local_intelligence"),  # Phase 22: attached to the draft as it is
    )
    if stream:
        await stream.flush()

    return {**state, "draft_itinerary": result.get("draft"), "builder_error": result.get("error")}


async def evaluate_node(state: OrchestratorState) -> OrchestratorState:
    draft = state.get("draft_itinerary")
    retry_count = state.get("evaluator_retry_count", 0)

    if draft is None:
        return {**state, "evaluator_verdict": {"passed": False, "failures": [], "retry_count": retry_count}}

    # Phase 21: the plan's total must fall within the range of its estimate — as wide as the season
    # makes prices move — and each hotel must be priced at what the search found.
    estimate = itinerary_estimate(
        draft,
        state.get("flights", []),
        state.get("hotels", []),
        destination=state.get("destination"),
        month=_travel_month(state),
    )
    verdict = await EvaluatorAgent().run(
        draft=draft,
        trip_start=state.get("start_date", ""),
        trip_end=state.get("end_date", ""),
        expected_budget_total=estimate.total,
        attractions=state.get("attractions", []),
        retry_count=retry_count,
        db=state.get("db"),
        trip_id=state.get("trip_id"),
        turn=state.get("turn", 1),
        budget_range=(estimate.total_min, estimate.total_max),
        hotels=state.get("hotels", []),
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


def _unmapped_activities(draft: dict) -> list[str]:
    """Activities the map cannot pin (Phase 18). Free-time slots are no place and don't count."""
    return [
        f"day {day.get('day')} {slot_name}: {slot['activity']}"
        for day in draft.get("days", [])
        for slot_name in SLOTS
        if (slot := day.get(slot_name))
        and slot.get("activity") != FREE_TIME
        and (slot.get("lat") is None or slot.get("lng") is None)
    ]


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

    # Phase 18: a missing coordinate is worth knowing about, never worth failing a trip for.
    unmapped = _unmapped_activities(draft)
    if unmapped:
        logger.warning("Itinerary for trip %s has activities without coordinates: %s", trip_id, unmapped)

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
        output={"itinerary_id": str(itinerary.id), "trip_status": "completed", "unmapped_activities": unmapped},
        duration_ms=timer.duration_ms,
    )

    # Phase 14: embeddings run in the background (own DB session, never raises) so
    # the embedding call and its retries cannot delay `planning_complete`.
    spawn(generate_embeddings(itinerary.id))

    return {**state, "itinerary_id": itinerary.id}


# A destination the tools could not use: these explain the failure better than "nothing found".
_DESTINATION_ERROR_CODES = ("OUTSIDE_COVERAGE", "NOT_FOUND")


def route_after_search(state: OrchestratorState) -> str:
    """With no flights, no hotel and nothing to do there is no trip to build — stop instead of saving an empty plan."""
    found = state.get("flights") or state.get("hotels") or state.get("attractions")
    return "build" if found else "nothing_found"


async def nothing_found_node(state: OrchestratorState) -> OrchestratorState:
    """Every search came back empty: fail the trip and say why (usually the destination)."""
    errors = [e for e in (state.get("activities_error"), state.get("hotel_error"), state.get("flight_error")) if e]
    destination_error = next((e for e in errors if e.get("code") in _DESTINATION_ERROR_CODES), None)
    reason = (destination_error or {}).get("error") or (
        f"No flights, places to stay or things to do were found for {state.get('destination') or 'this trip'}, "
        "so there is nothing to build a plan from."
    )

    async with timed_run() as timer:
        await _set_trip_status(state, TripStatus.FAILED)

    await _log(
        state,
        "nothing_found",
        input={"destination": state.get("destination")},
        output={"reason": reason, "errors": errors, "trip_status": "failed"},
        duration_ms=timer.duration_ms,
        status="failed",
    )
    await _publish(state, {"event": "planning_failed", "agent": "orchestrator", "status": "failed", "error": reason})
    return state


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
    graph.add_node("nothing_found", nothing_found_node)
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
    graph.add_conditional_edges(
        "hotel_activities", route_after_search, {"build": "build_itinerary", "nothing_found": "nothing_found"}
    )
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
    graph.add_edge("nothing_found", END)
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
    "budget_estimate": None,
    "local_intelligence": None,
    "intelligence_error": None,
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


def _keep_what_the_plan_uses(state: dict, plan: dict, *, hotels: bool, attractions: bool) -> None:
    """Narrow the builder's choices to what the current plan already uses.

    A targeted refinement changes one thing. The builder writes the whole plan
    again each time, and given the full lists it would re-choose the rest too:
    asking for different activities quietly swapped the hotel the traveller had
    just picked. A list is left alone when nothing in it is in the plan.
    """
    days = plan.get("days") or []
    if hotels:
        used = {(day.get("hotel") or {}).get("name") for day in days}
        state["hotels"] = [h for h in state.get("hotels") or [] if h.get("name") in used] or state.get("hotels", [])
    if attractions:
        used = {(day.get(slot) or {}).get("activity") for day in days for slot in SLOTS}
        state["attractions"] = [a for a in state.get("attractions") or [] if a.get("name") in used] or state.get(
            "attractions", []
        )


def _plan_outline(plan: dict) -> list[dict] | None:
    """A plan reduced to names — which place in which slot, which hotel — for the builder to keep to."""
    days = plan.get("days") or []
    return [
        {
            "date": day.get("date"),
            **{slot: (day.get(slot) or {}).get("activity") for slot in SLOTS},
            "hotel": (day.get("hotel") or {}).get("name"),
        }
        for day in days
    ] or None


# Which targeted pass repeats which search — POST /trips/{id}/retry names the agent (Phase 20).
RETRY_REFINEMENTS: dict[str, RefinementType] = {
    "flight_agent": "targeted_flights",
    "hotel_agent": "targeted_hotel",
    "activities_agent": "targeted_activities",
}
# What the builder is told on a retry, where a refinement carries the traveller's message.
_RETRY_REQUESTS: dict[str, str] = {
    "targeted_flights": "Flights have now been found: include the flight cost in the total. "
    "Keep every activity and the hotel exactly as they are.",
    "targeted_hotel": "A place to stay has now been found: add it to the plan. Keep every activity exactly as it is.",
    "targeted_activities": "Things to do have now been found: add them to the days. Keep the hotel exactly as it is.",
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
        "local_tips": bool(state.get("local_intelligence")),
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

        Local tips (Phase 22) are about the place, so they are carried forward
        by every refinement that keeps the destination; a plan that has none yet
        gets them beside the search a targeted refinement repeats.
        """
        if refinement_type in ("full_replan", "add_day"):
            fresh: dict = {k: prior_state.get(k) for k in TRIP_FIELDS}
            if refinement_type == "full_replan":
                fresh.update(raw_input=refinement_message, intent_override=True)
            else:
                fresh["end_date"] = _shift_date(fresh.get("end_date"), 1)
                # the same place, a day longer: what is true of it has not changed
                fresh.update({key: prior_state.get(key) for key in ("local_intelligence", "intelligence_error")})
            return await self.run(
                fresh, db=db, trip_id=trip_id, publish_fn=publish_fn, turn=turn, on_complete=on_complete
            )

        return await self._rerun_search(
            refinement_type,
            prior_state,
            refinement_message,
            db=db,
            trip_id=trip_id,
            publish_fn=publish_fn,
            turn=turn,
            on_complete=on_complete,
        )

    async def retry_search(
        self,
        agent_name: str,
        prior_state: dict,
        db: AsyncSession | None = None,
        trip_id: uuid.UUID | None = None,
        publish_fn=None,
        turn: int = 2,
        on_complete=None,
    ) -> dict:
        """Phase 20 — run one search again for a plan that was built without it.

        A plan is built from whatever was found, so a provider that was down
        leaves a plan with no flights (or no hotel, or nothing to do). This is
        the way back: the same targeted pass a refinement makes, with a fixed
        request in place of the traveller's. The plan keeps everything it has
        and gains what the search now finds. If the search fails again there is
        nothing to rebuild from — the run ends and says why.
        """
        refinement_type = RETRY_REFINEMENTS[agent_name]
        return await self._rerun_search(
            refinement_type,
            prior_state,
            _RETRY_REQUESTS[refinement_type],
            retry_of=agent_name,
            db=db,
            trip_id=trip_id,
            publish_fn=publish_fn,
            turn=turn,
            on_complete=on_complete,
        )

    async def _rerun_search(
        self,
        refinement_type: RefinementType,
        prior_state: dict,
        request: str,
        *,
        retry_of: str | None = None,
        db: AsyncSession | None = None,
        trip_id: uuid.UUID | None = None,
        publish_fn=None,
        turn: int = 2,
        on_complete=None,
    ) -> dict:
        """One search again, everything else carried forward, then build → evaluate → persist."""
        state: OrchestratorState = {
            **_RESULT_DEFAULTS,
            **prior_state,
            **_BUILD_RESET,
            "refinement_request": request,
            "previous_plan": _plan_outline(prior_state.get("draft_itinerary") or {}),
            "turn": turn,
            "db": db,
            "trip_id": trip_id,
            "publish_fn": publish_fn,
            "on_complete": on_complete,
        }

        _keep_what_the_plan_uses(
            state,
            prior_state.get("draft_itinerary") or {},
            hotels=refinement_type != "targeted_hotel",
            attractions=True,
        )

        async def search(run) -> dict:
            """The search — and, beside it, the local tips of a plan that has none yet (Phase 22)."""
            updates, tips = await asyncio.gather(run(state), _local_tips(state))
            state.update(tips)
            return updates

        async with timed_run() as timer:
            escalated = False
            if refinement_type == "targeted_flights":
                updates = await search(_search_flights)
                found, error = bool(updates["flights"]), updates["flight_error"]
                if found:  # a failed search keeps the previous flights
                    # The flights already found stay in the running (the plan takes the cheapest), so a
                    # request for something cheaper can never come back dearer when prices have moved.
                    known = [f for f in state.get("flights") or [] if f not in updates["flights"]]
                    updates["flights"] = updates["flights"] + known
                    # "Make it cheaper" → new flights, then the budget check decides whether the plan still stands.
                    state = await budget_decision_node({**state, **updates, "replan_attempts": 0})
                    escalated = route_after_budget_decision(state) == "escalate"
            elif refinement_type == "targeted_hotel":
                updates = await search(_search_hotels)
                found, error = bool(updates["hotels"]), updates["hotel_error"]
                if found:
                    state.update(updates)
            else:  # targeted_activities
                if retry_of is None:  # the traveller's message usually names the new interests
                    try:
                        new_interests = (await _extract_intent(request)).get("interests")
                    except Exception:
                        new_interests = None
                    if new_interests:
                        state["interests"] = new_interests
                updates = await search(_search_activities)
                found, error = bool(updates["attractions"]), updates["activities_error"]
                if found:
                    # the new finds join the places already in the plan: "add a food stop" must not
                    # cost the traveller every stop that is not food
                    names = {a.get("name") for a in updates["attractions"]}
                    updates["attractions"] = updates["attractions"] + [
                        a for a in state.get("attractions") or [] if a.get("name") not in names
                    ]
                    state.update(updates)

            if retry_of is not None and not found:
                # The search failed again, so the plan would come out as it is: don't rebuild it, say why.
                reason = failure_words(error) if error else "Nothing was found."
                await _publish(
                    state, {"event": "planning_failed", "agent": retry_of, "status": "failed", "error": reason}
                )
            elif escalated:
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
            input={"refinement_type": refinement_type, **({"retry": retry_of} if retry_of else {"message": request})},
            output=_result_summary(state),
            duration_ms=timer.duration_ms,
            status="completed" if state.get("itinerary_id") else "failed",
        )
        return _public(state)

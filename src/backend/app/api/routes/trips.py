"""
Trip routes — all require JWT.

GET    /trips                    list trips for user; optional ?status= filter
POST   /trips                    create a trip
GET    /trips/{id}               one trip
POST   /trips/{id}/plan          kick off planning (OrchestratorAgent), or ask a clarifying question
POST   /trips/{id}/clarify       answer a clarifying question / add detail, then plan
POST   /trips/{id}/replan        re-plan after a budget conflict with the chosen adjustment
POST   /trips/{id}/refine        multi-turn refinement — classify + targeted re-run (Phase 15)
GET    /trips/{id}/stream        SSE — live agent progress via Redis pub/sub
GET    /trips/{id}/status        polling fallback for the SSE stream (Phase 17)
GET    /trips/{id}/itinerary     latest itinerary for the trip
GET    /trips/{id}/itineraries   all itinerary versions, newest first (Phase 15)
GET    /trips/{id}/runs          all agent_runs for debugging; optional ?turn=N filter
GET    /trips/{id}/timeline      ordered event log: agent_runs + itineraries (Phase 13)
GET    /trips/{id}/similar       pgvector similarity (501 until Phase 23)

Planning runs as a background task (see _run_orchestrator): the HTTP call
returns immediately and progress arrives over SSE. Only one run per trip at a
time — starting another while one is in flight is a 409.
"""

import json
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_current_user, get_current_user_sse, get_redis_dep
from app.db.session import AsyncSessionLocal, get_db
from app.models.agent_run import AgentRun
from app.models.itinerary import Itinerary
from app.models.trip import Trip, TripStatus
from app.models.user import User
from app.schemas.agent_run import AgentRunRead
from app.schemas.itinerary import ItineraryRead
from app.schemas.trip import ClarifyRequest, PlanRequest, RefineRequest, ReplanRequest, TripCreate, TripRead
from app.schemas.types import as_utc
from src.ai.agents.budget_decision import viable_budget
from src.ai.agents.refinement_classifier import classify_refinement
from src.ai.orchestrator.orchestrator import TRIP_FIELDS, OrchestratorAgent
from src.ai.utils.conversation import (
    append_history,
    get_current_turn,
    get_history,
    get_trip_state,
    save_trip_state,
    start_history,
)
from src.ai.utils.tasks import spawn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trips", tags=["trips"])

SUB_AGENTS = ("flight_agent", "hotel_agent", "activities_agent")
INTERESTS_QUESTION = "What kinds of activities do you enjoy? (e.g. history, food, adventure, beach)"

# run(agent, db, hooks) → final orchestrator state; hooks = {"publish_fn": ..., "on_complete": ...}
OrchestratorCall = Callable[
    [OrchestratorAgent, AsyncSession, dict[str, Callable[[dict], Awaitable[None]]]], Awaitable[dict]
]


# ── Planning runs ──────────────────────────────────────────────────────────


def _events_channel(trip_id: uuid.UUID) -> str:
    return f"trip:{trip_id}:events"


def _trip_state(trip: Trip) -> dict:
    """The orchestrator's initial state for a trip row."""
    return {
        "destination": trip.destination,
        "start_date": str(trip.start_date),
        "end_date": str(trip.end_date),
        "budget": trip.budget,
        "group_size": trip.group_size,
        "interests": trip.interests or [],
    }


async def _run_orchestrator(
    trip_id: uuid.UUID, redis: aioredis.Redis, run: OrchestratorCall, turn: int = 1, has_itinerary: bool = False
) -> None:
    """Background task behind /plan, /clarify, /replan and /refine.

    The orchestrator calls `save_state` just before it publishes
    planning_complete, so the state POST /refine carries forward is in Redis by
    the time a client can ask for a refinement. Whatever happens, the trip
    never stays stuck in "planning": a crash marks it failed and tells the SSE
    stream. A trip that already has an itinerary (a refinement) falls back to
    "completed", because the previous itinerary still stands.
    """

    async def publish(event: dict) -> None:
        await redis.publish(_events_channel(trip_id), json.dumps(event, default=str))

    async def save_state(state: dict) -> None:
        await save_trip_state(redis, str(trip_id), {k: v for k, v in state.items() if k != "itinerary_id"})
        total = (state.get("draft_itinerary") or {}).get("total_cost") or 0
        await append_history(redis, str(trip_id), "assistant", f"Itinerary ready (₹{total:,.0f}).", turn)

    async with AsyncSessionLocal() as db:
        try:
            result = await run(OrchestratorAgent(), db, {"publish_fn": publish, "on_complete": save_state})
        except Exception as exc:
            logger.exception("Planning run crashed for trip %s", trip_id)
            fallback = TripStatus.COMPLETED if has_itinerary else TripStatus.FAILED
            try:
                await publish(
                    {"event": "planning_failed", "agent": "orchestrator", "status": "failed", "error": str(exc)}
                )
            except Exception:
                logger.warning("Could not publish planning_failed for trip %s", trip_id, exc_info=True)
        else:
            if result.get("itinerary_id"):
                return
            fallback = TripStatus.COMPLETED if has_itinerary else None  # the graph already marked it failed

        if fallback is not None:
            await db.rollback()
            trip = await db.get(Trip, trip_id)
            if trip is not None:
                trip.status = fallback
                await db.commit()


async def _start_run(
    trip: Trip, db: AsyncSession, redis: aioredis.Redis, run: OrchestratorCall, turn: int = 1, **event_extra: Any
) -> None:
    """Mark the trip as planning, announce it on the SSE channel, launch the run."""
    has_itinerary = trip.status == TripStatus.COMPLETED
    trip.status = TripStatus.PLANNING
    db.add(trip)
    await db.commit()

    await redis.publish(
        _events_channel(trip.id),
        json.dumps(
            {
                "event": "planning_started",
                "agent": "orchestrator",
                "status": "planning",
                "trip_id": str(trip.id),
                "turn": turn,
                **event_extra,
            }
        ),
    )
    spawn(_run_orchestrator(trip.id, redis, run, turn=turn, has_itinerary=has_itinerary))


def _ensure_not_planning(trip: Trip) -> None:
    if trip.status == TripStatus.PLANNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Planning is already in progress for this trip."
        )


async def _get_trip_or_404(trip_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> Trip:
    result = await db.execute(select(Trip).where(Trip.id == trip_id, Trip.user_id == user_id))
    trip = result.scalar_one_or_none()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found.")
    return trip


# ── GET /trips ─────────────────────────────────────────────────────────────


@router.get("", response_model=list[TripRead])
async def list_trips(
    status_filter: str | None = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Trip]:
    """List all trips for the authenticated user, newest first.

    Optional query parameter: ?status=pending|planning|completed|failed
    Unknown status values return an empty list rather than an error —
    consistent with a filter that matches nothing.
    """
    query = select(Trip).where(Trip.user_id == current_user.id)
    if status_filter is not None:
        query = query.where(Trip.status == status_filter)
    query = query.order_by(Trip.created_at.desc())
    result = await db.execute(query)
    return list(result.scalars().all())


# ── POST /trips ────────────────────────────────────────────────────────────


@router.post("", response_model=TripRead, status_code=201)
async def create_trip(
    body: TripCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Trip:
    trip = Trip(user_id=current_user.id, status=TripStatus.PENDING, **body.model_dump())
    db.add(trip)
    await db.commit()
    await db.refresh(trip)
    return trip


# ── GET /trips/{id} ────────────────────────────────────────────────────────


@router.get("/{trip_id}", response_model=TripRead)
async def get_trip(
    trip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Trip:
    return await _get_trip_or_404(trip_id, current_user.id, db)


# ── POST /trips/{id}/plan ──────────────────────────────────────────────────


@router.post("/{trip_id}/plan", status_code=202)
async def plan_trip(
    trip_id: uuid.UUID,
    body: PlanRequest = Body(default_factory=PlanRequest),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_dep),
):
    """Start planning — or ask instead of assume (Phase 7).

    A trip row always carries destination, dates and budget, so the only thing
    that can be missing is what the traveller enjoys. With no interests and no
    free text to infer them from, the answer is a question, not a plan:
    200 {"status": "clarification_needed", "question": ...} → POST /clarify.
    The check is plain Python — no LLM in the routing decision.
    """
    trip = await _get_trip_or_404(trip_id, current_user.id, db)
    _ensure_not_planning(trip)

    state = _trip_state(trip)
    if body.raw_input and body.raw_input.strip():
        state["raw_input"] = body.raw_input.strip()
    elif not trip.interests:
        await start_history(redis, str(trip_id), f"Plan a trip to {trip.destination}.")
        await append_history(redis, str(trip_id), "assistant", INTERESTS_QUESTION)
        return JSONResponse(
            status_code=200,
            content={"status": "clarification_needed", "question": INTERESTS_QUESTION, "trip_id": str(trip_id)},
        )

    await start_history(redis, str(trip_id), state.get("raw_input") or f"Plan a trip to {trip.destination}.")
    await _start_run(trip, db, redis, lambda agent, bg_db, hooks: agent.run(state, db=bg_db, trip_id=trip_id, **hooks))
    return {"status": "planning_started", "trip_id": str(trip_id)}


# ── POST /trips/{id}/clarify ───────────────────────────────────────────────


@router.post("/{trip_id}/clarify", status_code=200)
async def clarify_trip(
    trip_id: uuid.UUID,
    body: ClarifyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_dep),
) -> dict:
    """Plan with the user's answer as free text; intent parsing fills whatever was missing."""
    trip = await _get_trip_or_404(trip_id, current_user.id, db)
    _ensure_not_planning(trip)

    # Trip fields a previous turn already resolved (e.g. origin) are kept; old search results are not.
    saved = await get_trip_state(redis, str(trip_id)) or {}
    state = {**_trip_state(trip), **{k: saved[k] for k in TRIP_FIELDS if saved.get(k)}, "raw_input": body.answer}

    await append_history(redis, str(trip_id), "user", body.answer)
    await _start_run(trip, db, redis, lambda agent, bg_db, hooks: agent.run(state, db=bg_db, trip_id=trip_id, **hooks))
    return {"status": "planning_started", "trip_id": str(trip_id)}


# ── POST /trips/{id}/replan ────────────────────────────────────────────────


@router.post("/{trip_id}/replan", status_code=200)
async def replan_trip(
    trip_id: uuid.UUID,
    body: ReplanRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_dep),
) -> dict:
    """Re-plan after a budget conflict. The chosen adjustment is saved on the trip.

    "increase_budget" raises the budget to what the last flight search needs to
    pass the budget check (and by at least 25%), so the option is never a dead end.
    """
    trip = await _get_trip_or_404(trip_id, current_user.id, db)
    _ensure_not_planning(trip)

    replan_attempts = 0
    if body.choice == "cheaper_flights":
        replan_attempts = 1
    elif body.choice == "reduce_days":
        new_end = trip.end_date - timedelta(days=2)
        if new_end <= trip.start_date:
            raise HTTPException(status_code=422, detail="The trip is too short to remove 2 days.")
        trip.end_date = new_end
    else:
        last_check = (
            (
                await db.execute(
                    select(AgentRun)
                    .where(AgentRun.trip_id == trip_id, AgentRun.agent_name == "budget_decision")
                    .order_by(AgentRun.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        flight_cost = (last_check.output or {}).get("flight_cost") or 0 if last_check else 0
        trip.budget = max(round(trip.budget * 1.25, 2), viable_budget(flight_cost))

    state = {**_trip_state(trip), "replan_attempts": replan_attempts}
    await _start_run(
        trip,
        db,
        redis,
        lambda agent, bg_db, hooks: agent.run(state, db=bg_db, trip_id=trip_id, **hooks),
        choice=body.choice,
    )
    return {"status": "replanning_started", "trip_id": str(trip_id), "choice": body.choice}


# ── POST /trips/{id}/refine ────────────────────────────────────────────────


@router.post("/{trip_id}/refine")
async def refine_trip(
    trip_id: uuid.UUID,
    body: RefineRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_dep),
) -> dict:
    """Phase 15 — multi-turn refinement.

    Classifies the message into one of five action types (full_replan,
    targeted_flights, targeted_hotel, targeted_activities, add_day), then runs
    OrchestratorAgent.refine() in the background on the state the last
    successful run saved to Redis. No saved state → 409: there is nothing to
    refine until the trip has been planned once.

    Every agent_runs row written during this pass carries the new turn number.
    """
    trip = await _get_trip_or_404(trip_id, current_user.id, db)
    _ensure_not_planning(trip)

    prior_state = await get_trip_state(redis, str(trip_id))
    if not prior_state:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No prior planning state found. Run POST /plan first.",
        )

    turn = max(await get_current_turn(redis, str(trip_id)), 1) + 1  # the initial plan is always turn 1
    classification = await classify_refinement(body.message, await get_history(redis, str(trip_id)))
    await append_history(redis, str(trip_id), role="user", content=body.message, turn=turn)

    await _start_run(
        trip,
        db,
        redis,
        lambda agent, bg_db, hooks: agent.refine(
            refinement_type=classification.refinement_type,
            prior_state=prior_state,
            refinement_message=body.message,
            db=bg_db,
            trip_id=trip_id,
            turn=turn,
            **hooks,
        ),
        turn=turn,
        refinement_type=classification.refinement_type,
    )
    return {
        "status": "refinement_started",
        "trip_id": str(trip_id),
        "turn": turn,
        "refinement_type": classification.refinement_type,
        "reason": classification.reason,
    }


# ── GET /trips/{id}/stream ─────────────────────────────────────────────────


@router.get("/{trip_id}/stream")
async def stream_trip_events(
    trip_id: uuid.UUID,
    current_user: User = Depends(get_current_user_sse),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_dep),
) -> EventSourceResponse:
    trip = await _get_trip_or_404(trip_id, current_user.id, db)
    trip_status = trip.status
    await db.close()  # the stream can stay open for minutes — don't pin a pooled DB connection to it

    async def generator() -> AsyncGenerator:
        pubsub = redis.pubsub()
        await pubsub.subscribe(_events_channel(trip_id))
        # Pub/sub has no replay: `trip_status` tells a late (re)connecting client where the run stands.
        yield {
            "event": "connected",
            "data": json.dumps({"trip_id": str(trip_id), "status": "listening", "trip_status": trip_status}),
        }
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    yield {"event": "agent_update", "data": message["data"]}
        finally:
            await pubsub.unsubscribe(_events_channel(trip_id))
            await pubsub.aclose()

    return EventSourceResponse(generator())


# ── GET /trips/{id}/status ─────────────────────────────────────────────────


@router.get("/{trip_id}/status")
async def get_trip_status(
    trip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Current planning status + per-agent progress (SSE polling fallback).

    Response:
        {
            "status": "pending" | "planning" | "completed" | "failed",
            "trip_id": "<uuid>",
            "progress": {
                "agents_done": 0–3,
                "agents_total": 3,
                "agents": {"flight_agent": "pending" | "completed" | "failed", ...}
            }
        }

    Derived from agent_runs — no extra state to keep in sync. While a run is in
    flight only its own rows count, so a re-plan starts again from 0/3 instead
    of showing the previous run's results.
    """
    trip = await _get_trip_or_404(trip_id, current_user.id, db)

    result = await db.execute(select(AgentRun).where(AgentRun.trip_id == trip_id).order_by(AgentRun.created_at.asc()))
    runs = list(result.scalars().all())
    if trip.status == TripStatus.PLANNING:
        # Every run ends with an "orchestrator" row; whatever follows the last one is the run in flight.
        last_finished = max((i for i, run in enumerate(runs) if run.agent_name == "orchestrator"), default=-1)
        runs = runs[last_finished + 1 :]

    agents = dict.fromkeys(SUB_AGENTS, "pending")
    agents.update({run.agent_name: run.status for run in runs if run.agent_name in SUB_AGENTS})  # latest wins

    return {
        "status": trip.status,
        "trip_id": str(trip_id),
        "progress": {
            "agents_done": sum(1 for s in agents.values() if s == "completed"),
            "agents_total": len(SUB_AGENTS),
            "agents": agents,
        },
    }


# ── GET /trips/{id}/itinerary ──────────────────────────────────────────────


@router.get("/{trip_id}/itinerary", response_model=ItineraryRead)
async def get_itinerary(
    trip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Itinerary:
    await _get_trip_or_404(trip_id, current_user.id, db)
    result = await db.execute(
        select(Itinerary).where(Itinerary.trip_id == trip_id).order_by(Itinerary.created_at.desc()).limit(1)
    )
    itinerary = result.scalar_one_or_none()
    if not itinerary:
        raise HTTPException(status_code=404, detail="No itinerary has been generated for this trip yet.")
    return itinerary


# ── GET /trips/{id}/itineraries ────────────────────────────────────────────


@router.get("/{trip_id}/itineraries", response_model=list[ItineraryRead])
async def list_itineraries(
    trip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Itinerary]:
    """Phase 15 — all itinerary versions for a trip, newest first.

    Each planning turn (initial + every refinement) produces one new row.
    This endpoint returns them all so callers can diff turn 1 vs turn 2,
    or let the user roll back to an earlier version.
    """
    await _get_trip_or_404(trip_id, current_user.id, db)
    result = await db.execute(
        select(Itinerary).where(Itinerary.trip_id == trip_id).order_by(Itinerary.created_at.desc())
    )
    return list(result.scalars().all())


# ── GET /trips/{id}/similar ────────────────────────────────────────────────


@router.get("/{trip_id}/similar")
async def get_similar_trips(
    trip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _get_trip_or_404(trip_id, current_user.id, db)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Similarity search — Phase 23.")


# ── GET /trips/{id}/runs ───────────────────────────────────────────────────


@router.get("/{trip_id}/runs", response_model=list[AgentRunRead])
async def get_trip_runs(
    trip_id: uuid.UUID,
    turn: int | None = Query(
        default=None, ge=1, description="Filter by conversation turn (1-indexed). Omit for all turns."
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentRun]:
    """Phase 15: optional ?turn=N filters to a specific refinement pass."""
    await _get_trip_or_404(trip_id, current_user.id, db)
    query = select(AgentRun).where(AgentRun.trip_id == trip_id)
    if turn is not None:
        query = query.where(AgentRun.turn == turn)
    query = query.order_by(AgentRun.created_at.asc())
    result = await db.execute(query)
    return list(result.scalars().all())


# ── GET /trips/{id}/timeline ───────────────────────────────────────────────

_AGENT_LABELS: dict[str, str] = {
    "intent_parsing": "Extracted trip details from user input",
    "preferences": "Applied saved traveller preferences",
    "flight_agent": "Searched for flights",
    "budget_decision": "Evaluated budget feasibility",
    "hotel_agent": "Searched for hotels",
    "activities_agent": "Searched for activities and attractions",
    "itinerary_builder": "Built day-by-day itinerary",
    "evaluator": "Validated itinerary correctness",
    "persist": "Saved itinerary to database",
    "escalate": "Budget conflict — offered alternatives to user",
    "builder_failed": "Itinerary build failed after maximum retries",
    "preference_extractor": "Learned traveller preferences from the trip",
    "orchestrator": "Finished planning run",
}

_STATUS_ICONS: dict[str, str] = {"completed": "✓", "failed": "✗", "pending": "…"}


def _agent_label(agent_name: str, run_status: str) -> str:
    icon = _STATUS_ICONS.get(run_status, "?")
    label = _AGENT_LABELS.get(agent_name, agent_name.replace("_", " ").title())
    return f"{icon} {label}"


@router.get("/{trip_id}/timeline")
async def get_trip_timeline(
    trip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Ordered event log for debugging — agent_runs interleaved with itinerary saves.

    Each entry has:
      - event_type: "agent_run" | "itinerary_saved"
      - label:      human-readable description
      - timestamp:  ISO 8601 UTC string
      - status:     "completed" | "failed" | "pending"
      - detail:     agent-specific summary (cost, count, error code, etc.)

    Entries are sorted by created_at ascending so you can read a run top-to-bottom.
    This endpoint is intentionally not paginated — a single trip run produces
    at most ~15 rows, well within the response budget.
    """
    await _get_trip_or_404(trip_id, current_user.id, db)

    runs = (await db.execute(select(AgentRun).where(AgentRun.trip_id == trip_id))).scalars().all()
    itineraries = (await db.execute(select(Itinerary).where(Itinerary.trip_id == trip_id))).scalars().all()

    events: list[dict[str, Any]] = [
        {
            "event_type": "agent_run",
            "agent_name": run.agent_name,
            "label": _agent_label(run.agent_name, run.status),
            "timestamp": as_utc(run.created_at).isoformat(),
            "status": run.status,
            "turn": run.turn,
            "duration_ms": run.duration_ms,
            "detail": _run_detail(run.agent_name, run.output or {}, run.status),
        }
        for run in runs
    ]
    events += [
        {
            "event_type": "itinerary_saved",
            "agent_name": None,
            "label": f"✓ Itinerary saved (₹{itinerary.total_cost:,.0f})"
            if itinerary.total_cost
            else "✓ Itinerary saved",
            "timestamp": as_utc(itinerary.created_at).isoformat(),
            "status": "completed",
            "turn": None,
            "duration_ms": None,
            "detail": {
                "itinerary_id": str(itinerary.id),
                "total_cost": itinerary.total_cost,
                "has_structured_data": itinerary.structured_data is not None,
            },
        }
        for itinerary in itineraries
    ]
    events.sort(key=lambda e: e["timestamp"])
    return events


def _run_detail(agent_name: str, output: dict, run_status: str) -> dict[str, Any]:
    """Extract a human-readable summary dict from an agent_runs output field."""
    error = output.get("error")
    if run_status == "failed" and error:
        if isinstance(error, dict):
            return {"error_code": error.get("code"), "error_message": error.get("error")}
        return {"error": str(error)}

    if agent_name == "flight_agent":
        flights = output.get("flights", [])
        return {
            "flights_found": len(flights),
            "cheapest_inr": min((f.get("price_inr", 0) for f in flights), default=None),
        }

    if agent_name == "hotel_agent":
        return {"hotels_found": len(output.get("hotels", []))}

    if agent_name == "activities_agent":
        return {"attractions_found": len(output.get("attractions", []))}

    if agent_name == "budget_decision":
        return {
            "decision": output.get("decision"),
            "flight_cost": output.get("flight_cost"),
            "remaining_budget": output.get("remaining_budget"),
        }

    if agent_name == "evaluator":
        failures = output.get("failures", [])
        return {
            "passed": output.get("passed"),
            "failure_count": len(failures),
            "failure_types": [f.get("check") for f in failures],
        }

    if agent_name == "itinerary_builder":
        draft = output.get("draft") or {}
        return {"days_count": len(draft.get("days", [])), "total_cost": draft.get("total_cost")}

    if agent_name == "persist":
        return {"itinerary_id": output.get("itinerary_id"), "trip_status": output.get("trip_status")}

    if agent_name == "intent_parsing":
        return {
            "pass_through": output.get("pass_through", False),
            "fields_extracted": list(output.get("extracted_fields", {})),
        }

    if agent_name == "orchestrator":
        return {
            "flights_count": output.get("flights_count", 0),
            "hotels_count": output.get("hotels_count", 0),
            "attractions_count": output.get("attractions_count", 0),
            "itinerary_id": output.get("itinerary_id"),
        }

    return output

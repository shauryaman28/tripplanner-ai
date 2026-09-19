"""
Trip routes — all require JWT.

Phase 13 additions:
  - GET /trips now accepts optional ?status= filter
  - GET /trips/{id}/timeline — ordered event log (internal debug endpoint)

Phase 15 additions:
  - GET /trips/{id}/runs accepts optional ?turn=N filter
  - POST /trips/{id}/refine — multi-turn refinement (classifies + re-runs targeted agents)
  - GET /trips/{id}/itineraries — all itinerary versions, newest first
  - Planning state persisted to Redis after run so /refine can load it

GET    /trips                    list trips for user; optional ?status= filter
POST   /trips                    create a trip
POST   /trips/{id}/plan          kick off planning (OrchestratorAgent)
GET    /trips/{id}/stream        SSE — live agent progress via Redis pub/sub
GET    /trips/{id}/itinerary     latest itinerary for the trip
GET    /trips/{id}/itineraries   all itinerary versions, newest first (Phase 15)
GET    /trips/{id}/similar       pgvector similarity (501 until Phase 23)
GET    /trips/{id}/runs          all agent_runs for debugging; optional ?turn=N filter
POST   /trips/{id}/replan        re-trigger with budget conflict choice
GET    /trips/{id}/timeline      ordered event log: agent_runs + itinerary (Phase 13)
POST   /trips/{id}/refine        multi-turn refinement — classify + targeted re-run (Phase 15)
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel
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
from app.schemas.trip import ClarifyRequest, PlanRequest, ReplanRequest, TripCreate, TripRead
from src.ai.orchestrator.orchestrator import OrchestratorAgent
from src.ai.utils.conversation import append_history, get_current_turn, get_history, get_trip_state, save_trip_state

router = APIRouter(prefix="/trips", tags=["trips"])

# ── Background task GC protection ─────────────────────────────────────────

_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ── Human-readable labels for agent_runs rows (Phase 13 timeline) ─────────

_AGENT_LABELS: dict[str, str] = {
    "intent_parsing": "Extracted trip details from user input",
    "flight_agent": "Searched for flights",
    "budget_decision": "Evaluated budget feasibility",
    "hotel_agent": "Searched for hotels",
    "activities_agent": "Searched for activities and attractions",
    "itinerary_builder": "Built day-by-day itinerary",
    "evaluator": "Validated itinerary correctness",
    "persist": "Saved itinerary to database",
    "escalate": "Budget conflict — offered alternatives to user",
    "builder_failed": "Itinerary build failed after maximum retries",
    "orchestrator": "Completed full planning run",
}

_STATUS_LABELS: dict[str, str] = {
    "completed": "✓",
    "failed": "✗",
    "pending": "…",
}


def _agent_label(agent_name: str, run_status: str) -> str:
    icon = _STATUS_LABELS.get(run_status, "?")
    label = _AGENT_LABELS.get(agent_name, agent_name.replace("_", " ").title())
    return f"{icon} {label}"


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
    trip = Trip(
        user_id=current_user.id,
        destination=body.destination,
        start_date=body.start_date,
        end_date=body.end_date,
        budget=body.budget,
        group_size=body.group_size,
        interests=body.interests,
        status=TripStatus.PENDING,
    )
    db.add(trip)
    await db.commit()
    await db.refresh(trip)
    return trip


# ── POST /trips/{id}/plan ──────────────────────────────────────────────────


@router.post("/{trip_id}/plan", status_code=202)
async def plan_trip(
    trip_id: uuid.UUID,
    body: PlanRequest = Body(default_factory=PlanRequest),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_dep),
) -> dict:
    trip = await _get_trip_or_404(trip_id, current_user.id, db)

    initial_state: dict = {
        "destination": trip.destination,
        "start_date": str(trip.start_date),
        "end_date": str(trip.end_date),
        "budget": trip.budget,
        "group_size": trip.group_size,
        "interests": trip.interests or [],
    }
    if body.raw_input:
        initial_state["raw_input"] = body.raw_input

    trip.status = TripStatus.PLANNING
    db.add(trip)
    await db.commit()

    await redis.publish(
        f"trip:{trip_id}:events",
        json.dumps({
            "event": "planning_started",
            "agent": "orchestrator",
            "status": "planning",
            "trip_id": str(trip_id),
        }),
    )

    async def publish_fn(event: dict) -> None:
        await redis.publish(f"trip:{trip_id}:events", json.dumps(event))

    _fire_and_forget(_run_orchestrator(
        trip_id=trip_id,
        initial_state=initial_state,
        publish_fn=publish_fn,
        redis=redis,
    ))

    return {"status": "planning_started", "trip_id": str(trip_id)}


async def _run_orchestrator(trip_id, initial_state, publish_fn, redis=None) -> None:
    async with AsyncSessionLocal() as db:
        try:
            agent = OrchestratorAgent()
            result = await agent.run(initial_state, db=db, trip_id=trip_id, publish_fn=publish_fn)
            # Phase 15: persist final state to Redis so POST /refine can load it.
            if redis is not None:
                state_to_save = {k: v for k, v in result.items()
                                 if k not in ("db", "trip_id", "publish_fn", "itinerary_id")}
                await save_trip_state(redis, str(trip_id), state_to_save)
        except Exception as exc:
            try:
                await publish_fn({
                    "event": "planning_failed",
                    "agent": "orchestrator",
                    "status": "failed",
                    "error": str(exc),
                })
            except Exception:
                pass


# ── POST /trips/{id}/clarify ───────────────────────────────────────────────


@router.post("/{trip_id}/clarify", status_code=200)
async def clarify_trip(
    trip_id: uuid.UUID,
    body: ClarifyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_dep),
) -> dict:
    trip = await _get_trip_or_404(trip_id, current_user.id, db)

    prev_state: dict = await get_trip_state(redis, str(trip_id)) or {}
    prev_state.pop("clarification_question", None)
    prev_state.pop("raw_input", None)
    prev_state["raw_input"] = body.answer

    await append_history(redis, str(trip_id), "user", body.answer)

    trip.status = TripStatus.PLANNING
    db.add(trip)
    await db.commit()

    await redis.publish(
        f"trip:{trip_id}:events",
        json.dumps({
            "event": "planning_started",
            "agent": "orchestrator",
            "status": "planning",
            "trip_id": str(trip_id),
        }),
    )

    async def publish_fn(event: dict) -> None:
        await redis.publish(f"trip:{trip_id}:events", json.dumps(event))

    _fire_and_forget(_run_orchestrator(trip_id=trip_id, initial_state=prev_state, publish_fn=publish_fn))

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
    trip = await _get_trip_or_404(trip_id, current_user.id, db)

    initial_state: dict = {
        "destination": trip.destination,
        "start_date": str(trip.start_date),
        "end_date": str(trip.end_date),
        "budget": trip.budget,
        "group_size": trip.group_size,
        "interests": trip.interests or [],
        "replan_attempts": 0,
    }

    if body.choice == "cheaper_flights":
        initial_state["replan_attempts"] = 1
    elif body.choice == "reduce_days":
        new_end = trip.end_date - timedelta(days=2)
        initial_state["end_date"] = str(new_end)
    elif body.choice == "increase_budget":
        initial_state["budget"] = round(trip.budget * 1.25, 2)

    trip.status = TripStatus.PLANNING
    db.add(trip)
    await db.commit()

    await redis.publish(
        f"trip:{trip_id}:events",
        json.dumps({
            "event": "planning_started",
            "agent": "orchestrator",
            "status": "planning",
            "trip_id": str(trip_id),
            "choice": body.choice,
        }),
    )

    async def publish_fn(event: dict) -> None:
        await redis.publish(f"trip:{trip_id}:events", json.dumps(event))

    _fire_and_forget(_run_orchestrator(trip_id=trip_id, initial_state=initial_state, publish_fn=publish_fn))

    return {"status": "replanning_started", "trip_id": str(trip_id), "choice": body.choice}


# ── GET /trips/{id}/stream ─────────────────────────────────────────────────


@router.get("/{trip_id}/stream")
async def stream_trip_events(
    trip_id: uuid.UUID,
    current_user: User = Depends(get_current_user_sse),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_dep),
) -> EventSourceResponse:
    await _get_trip_or_404(trip_id, current_user.id, db)

    async def generator() -> AsyncGenerator:
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"trip:{trip_id}:events")
        yield {"event": "connected", "data": json.dumps({"trip_id": str(trip_id), "status": "listening"})}
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    yield {"event": "agent_update", "data": message["data"]}
        finally:
            await pubsub.unsubscribe(f"trip:{trip_id}:events")
            await pubsub.aclose()

    return EventSourceResponse(generator())


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


# ── GET /trips/{id}/itineraries ───────────────────────────────────────────────


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
        select(Itinerary)
        .where(Itinerary.trip_id == trip_id)
        .order_by(Itinerary.created_at.desc())
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
    turn: int | None = Query(default=None, description="Filter by conversation turn (1-indexed). Omit for all turns."),
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

    runs_result = await db.execute(
        select(AgentRun).where(AgentRun.trip_id == trip_id).order_by(AgentRun.created_at.asc())
    )
    runs = runs_result.scalars().all()

    itineraries_result = await db.execute(
        select(Itinerary).where(Itinerary.trip_id == trip_id).order_by(Itinerary.created_at.asc())
    )
    itineraries = itineraries_result.scalars().all()

    events: list[dict[str, Any]] = []

    for run in runs:
        events.append({
            "event_type": "agent_run",
            "agent_name": run.agent_name,
            "label": _agent_label(run.agent_name, run.status),
            "timestamp": run.created_at.isoformat() if run.created_at else None,
            "status": run.status,
            "duration_ms": run.duration_ms,
            "detail": _run_detail(run.agent_name, run.output or {}, run.status),
        })

    for itinerary in itineraries:
        events.append({
            "event_type": "itinerary_saved",
            "agent_name": None,
            "label": f"✓ Itinerary saved (₹{itinerary.total_cost:,.0f})" if itinerary.total_cost else "✓ Itinerary saved",
            "timestamp": itinerary.created_at.isoformat() if itinerary.created_at else None,
            "status": "completed",
            "duration_ms": None,
            "detail": {
                "itinerary_id": str(itinerary.id),
                "total_cost": itinerary.total_cost,
                "has_structured_data": itinerary.structured_data is not None,
            },
        })

    events.sort(key=lambda e: e["timestamp"] or "")

    return events


def _run_detail(agent_name: str, output: dict, run_status: str) -> dict[str, Any]:
    """Extract a human-readable summary dict from an agent_runs output field."""
    if run_status == "failed":
        error = output.get("error") or {}
        if isinstance(error, dict):
            return {"error_code": error.get("code"), "error_message": error.get("error")}
        return {"error": str(error)}

    if agent_name == "flight_agent":
        flights = output.get("flights", [])
        return {"flights_found": len(flights), "cheapest_inr": min((f.get("price_inr", 0) for f in flights), default=None)}

    if agent_name == "hotel_agent":
        hotels = output.get("hotels", [])
        return {"hotels_found": len(hotels)}

    if agent_name == "activities_agent":
        attractions = output.get("attractions", [])
        return {"attractions_found": len(attractions)}

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
        return {
            "days_count": len(draft.get("days", [])),
            "total_cost": draft.get("total_cost"),
        }

    if agent_name == "persist":
        return {
            "itinerary_id": output.get("itinerary_id"),
            "trip_status": output.get("trip_status"),
        }

    if agent_name == "intent_parsing":
        extracted = output.get("extracted_fields", {})
        return {
            "pass_through": output.get("pass_through", False),
            "fields_extracted": [k for k, v in extracted.items() if v is not None],
        }

    if agent_name == "orchestrator":
        return {
            "flights_count": output.get("flights_count", 0),
            "hotels_count": output.get("hotels_count", 0),
            "attractions_count": output.get("attractions_count", 0),
            "itinerary_id": output.get("itinerary_id"),
        }

    return output


# ── Private helpers ────────────────────────────────────────────────────────


async def _get_trip_or_404(trip_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> Trip:
    result = await db.execute(select(Trip).where(Trip.id == trip_id, Trip.user_id == user_id))
    trip = result.scalar_one_or_none()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found.")
    return trip


# ── POST /trips/{id}/refine ────────────────────────────────────────────────


class RefineRequest(BaseModel):
    message: str


@router.post("/{trip_id}/refine")
async def refine_trip(
    trip_id: uuid.UUID,
    body: RefineRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis_dep),
) -> dict:
    """Phase 15 — multi-turn refinement.

    Classifies the user's refinement message into one of five action types
    (full_replan, targeted_flights, targeted_hotel, targeted_activities, add_day),
    then kicks off OrchestratorAgent.refine() as a background task.

    Prior planning state (flights, hotels, attractions, trip metadata) is loaded
    from Redis. If no prior state exists the call returns 409 — the trip must
    have been planned at least once before refinement is possible.

    The turn number is derived from the conversation history (current_turn + 1)
    so every agent_runs row written during this pass is labelled correctly.
    """
    trip = await _get_trip_or_404(trip_id, current_user.id, db)

    # Load prior state — required for carry-forward logic
    prior_state = await get_trip_state(r, str(trip_id))
    if not prior_state:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No prior planning state found. Run POST /plan first.",
        )

    # Determine the next turn number from conversation history
    current_turn = await get_current_turn(r, str(trip_id))
    next_turn = current_turn + 1

    # Classify the refinement message
    from src.ai.agents.refinement_classifier import classify_refinement
    from src.ai.utils.conversation import get_history

    history = await get_history(r, str(trip_id))
    classification = await classify_refinement(body.message, history)

    # Record user message in conversation history
    await append_history(r, str(trip_id), role="user", content=body.message, turn=next_turn)

    async def _refine_task() -> None:
        async with AsyncSessionLocal() as bg_db:
            agent = OrchestratorAgent()
            await agent.refine(
                refinement_type=classification.refinement_type,
                prior_state=prior_state,
                refinement_message=body.message,
                db=bg_db,
                trip_id=trip_id,
                turn=next_turn,
            )

    task = asyncio.create_task(_refine_task())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {
        "status": "refinement_started",
        "trip_id": str(trip_id),
        "turn": next_turn,
        "refinement_type": classification.refinement_type,
        "reason": classification.reason,
    }

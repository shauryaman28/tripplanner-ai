"""
Trip routes — all require JWT.

GET    /trips                    list all trips for authenticated user
POST   /trips                    create a trip
POST   /trips/{id}/plan          kick off planning (Phase 9: OrchestratorAgent)
GET    /trips/{id}/stream        SSE — live agent progress via Redis pub/sub
GET    /trips/{id}/itinerary     latest itinerary for the trip
GET    /trips/{id}/similar       pgvector similarity (501 until Phase 23)
GET    /trips/{id}/runs          all agent_runs for debugging
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_current_user, get_current_user_sse, get_redis_dep
from app.db.session import get_db
from app.models.agent_run import AgentRun
from app.models.itinerary import Itinerary
from app.models.trip import Trip, TripStatus
from app.models.user import User
from app.schemas.agent_run import AgentRunRead
from app.schemas.itinerary import ItineraryRead
from app.schemas.trip import ClarifyRequest, PlanRequest, TripCreate, TripRead
from src.ai.orchestrator.orchestrator import OrchestratorAgent
from src.ai.utils.conversation import (
    append_history,
    get_trip_state,
    save_trip_state,
)

router = APIRouter(prefix="/trips", tags=["trips"])


# ── GET /trips ─────────────────────────────────────────────────────────────


@router.get("", response_model=list[TripRead])
async def list_trips(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Trip]:
    """Return all trips belonging to the authenticated user, newest first."""
    result = await db.execute(select(Trip).where(Trip.user_id == current_user.id).order_by(Trip.created_at.desc()))
    return list(result.scalars().all())


# ── POST /trips ────────────────────────────────────────────────────────────


@router.post("", response_model=TripRead, status_code=201)
async def create_trip(
    body: TripCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Trip:
    """Create a new trip. Authenticated user is the owner."""
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
    """
    Kick off trip planning via OrchestratorAgent (Phase 9).

    - Publishes planning_started SSE event immediately.
    - Runs OrchestratorAgent: intent parsing → concurrent fan-out to
      FlightAgent, HotelAgent, ActivitiesAgent → merge.
    - Each sub-agent publishes its own SSE progress event on completion.
    - Publishes planning_complete SSE event when the Orchestrator finishes.
    - Runs in a background task so the HTTP response returns 202 immediately.
    """
    trip = await _get_trip_or_404(trip_id, current_user.id, db)

    # Build initial state from the trip row; raw_input overlays if present
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

    # Mark trip as planning
    trip.status = TripStatus.PLANNING
    db.add(trip)
    await db.commit()

    # Publish planning_started immediately so SSE clients see it at once
    await redis.publish(
        f"trip:{trip_id}:events",
        json.dumps({
            "event": "planning_started",
            "agent": "orchestrator",
            "status": "planning",
            "trip_id": str(trip_id),
        }),
    )

    # Build SSE publish helper that serialises events to the Redis channel
    async def publish_fn(event: dict) -> None:
        await redis.publish(
            f"trip:{trip_id}:events",
            json.dumps(event),
        )

    # Run the Orchestrator in a background task so we return 202 immediately
    asyncio.create_task(
        _run_orchestrator(
            trip_id=trip_id,
            initial_state=initial_state,
            db=db,
            redis=redis,
            publish_fn=publish_fn,
        )
    )

    return {"status": "planning_started", "trip_id": str(trip_id)}


async def _run_orchestrator(
    trip_id: uuid.UUID,
    initial_state: dict,
    db: AsyncSession,
    redis: aioredis.Redis,
    publish_fn,
) -> None:
    """Background task: run OrchestratorAgent and publish final status."""
    try:
        agent = OrchestratorAgent()
        await agent.run(
            initial_state,
            db=db,
            trip_id=trip_id,
            publish_fn=publish_fn,
        )
    except Exception as exc:
        # Surface unhandled orchestrator exceptions via SSE so the frontend
        # doesn't hang waiting for a planning_complete event that never arrives.
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
    """
    Submit the user's answer to a clarifying question and re-trigger planning.

    Loads partial state saved by a previous /plan call, merges the answer
    as raw_input, and re-runs the OrchestratorAgent from the top.
    """
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

    asyncio.create_task(
        _run_orchestrator(
            trip_id=trip_id,
            initial_state=prev_state,
            db=db,
            redis=redis,
            publish_fn=publish_fn,
        )
    )

    return {"status": "planning_started", "trip_id": str(trip_id)}


# ── GET /trips/{id}/stream ─────────────────────────────────────────────────


@router.get("/{trip_id}/stream")
async def stream_trip_events(
    trip_id: uuid.UUID,
    current_user: User = Depends(get_current_user_sse),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_dep),
) -> EventSourceResponse:
    """
    Server-Sent Events stream for live agent progress.

    Subscribes to Redis channel trip:<id>:events and forwards every
    message as an SSE event.  Browsers connect via:
        new EventSource('/trips/<id>/stream?token=<jwt>')
    """
    await _get_trip_or_404(trip_id, current_user.id, db)

    async def generator() -> AsyncGenerator:
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"trip:{trip_id}:events")
        yield {
            "event": "connected",
            "data": json.dumps({"trip_id": str(trip_id), "status": "listening"}),
        }
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
    """Return the most recently generated itinerary for the trip."""
    await _get_trip_or_404(trip_id, current_user.id, db)

    result = await db.execute(
        select(Itinerary).where(Itinerary.trip_id == trip_id).order_by(Itinerary.created_at.desc()).limit(1)
    )
    itinerary = result.scalar_one_or_none()
    if not itinerary:
        raise HTTPException(
            status_code=404,
            detail="No itinerary has been generated for this trip yet.",
        )
    return itinerary


# ── GET /trips/{id}/similar ────────────────────────────────────────────────


@router.get("/{trip_id}/similar")
async def get_similar_trips(
    trip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """pgvector similarity search — implemented in Phase 23."""
    await _get_trip_or_404(trip_id, current_user.id, db)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Similarity search will be wired in Phase 23 (pgvector).",
    )


# ── GET /trips/{id}/runs ───────────────────────────────────────────────────


@router.get("/{trip_id}/runs", response_model=list[AgentRunRead])
async def get_trip_runs(
    trip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentRun]:
    """Return all agent_run rows for a trip, oldest first.

    This is the primary debugging endpoint — use it to trace every
    agent decision without writing extra logging code.
    """
    await _get_trip_or_404(trip_id, current_user.id, db)

    result = await db.execute(select(AgentRun).where(AgentRun.trip_id == trip_id).order_by(AgentRun.created_at.asc()))
    return list(result.scalars().all())


# ── Private helpers ────────────────────────────────────────────────────────


async def _get_trip_or_404(trip_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> Trip:
    result = await db.execute(select(Trip).where(Trip.id == trip_id, Trip.user_id == user_id))
    trip = result.scalar_one_or_none()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found.")
    return trip

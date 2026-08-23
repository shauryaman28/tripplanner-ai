"""
Trip routes — all require JWT.

GET    /trips                    list all trips for authenticated user
POST   /trips                    create a trip
POST   /trips/{id}/plan          kick off planning (Phase 9: OrchestratorAgent)
GET    /trips/{id}/stream        SSE — live agent progress via Redis pub/sub
GET    /trips/{id}/itinerary     latest itinerary for the trip
GET    /trips/{id}/similar       pgvector similarity (501 until Phase 23)
GET    /trips/{id}/runs          all agent_runs for debugging
POST   /trips/{id}/replan        Phase 10: re-trigger with budget conflict choice
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta

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
from app.schemas.trip import ClarifyRequest, PlanRequest, ReplanRequest, TripCreate, TripRead
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
    result = await db.execute(select(Trip).where(Trip.user_id == current_user.id).order_by(Trip.created_at.desc()))
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


async def _run_orchestrator(trip_id, initial_state, db, redis, publish_fn) -> None:
    try:
        agent = OrchestratorAgent()
        await agent.run(initial_state, db=db, trip_id=trip_id, publish_fn=publish_fn)
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


# ── POST /trips/{id}/replan — Phase 10 ────────────────────────────────────


@router.post("/{trip_id}/replan", status_code=200)
async def replan_trip(
    trip_id: uuid.UUID,
    body: ReplanRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_dep),
) -> dict:
    """Re-trigger planning with a user-chosen budget conflict resolution.

    cheaper_flights  → start replan_attempts at 1 (65% budget cap on flights)
    reduce_days      → shorten trip by 2 days, re-run from scratch
    increase_budget  → add 25% to total budget, re-run from scratch
    """
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
        # Skip straight to replan-budget logic on first flight run
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

    asyncio.create_task(
        _run_orchestrator(
            trip_id=trip_id,
            initial_state=initial_state,
            db=db,
            redis=redis,
            publish_fn=publish_fn,
        )
    )

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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentRun]:
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

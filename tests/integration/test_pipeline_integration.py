"""End-to-end backend test — Phases 5–18 through the public HTTP API.

Real Postgres (Alembic schema), real Redis, the real LangGraph orchestrator,
real background tasks. Only the external network seams are stubbed: the MCP
tool calls, the builder / intent LLMs and the OpenAI embedding call.

    RUN_INTEGRATION=1 pytest tests/integration/test_pipeline_integration.py -v
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from app.core.config import settings
from app.models.embedding import Embedding
from src.ai.agents.refinement_classifier import RefinementClassification
from tests.fakes import flight, network_stubs

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

START = date.today() + timedelta(days=30)
END = START + timedelta(days=2)

BEACH_HOTELS = [
    {"name": "Beach House", "stars": 5, "price_per_night_inr": 6000.0, "rating": 4.8, "address": "Baga Beach"}
]


@asynccontextmanager
async def _stack(flight_tool, hotel_tool=None):
    """The running app with its network seams stubbed. Yields (client, events, tool mocks)."""
    from app.db.redis import close_redis, init_redis
    from app.main import app

    events: list[dict] = []
    listener = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = listener.pubsub()
    await pubsub.psubscribe("trip:*:events")

    async def collect() -> None:
        async for message in pubsub.listen():
            if message["type"] == "pmessage":
                events.append(json.loads(message["data"]))

    collector = asyncio.create_task(collect())
    await init_redis()
    try:
        with network_stubs(flight_tool, hotel_tool) as tools:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                yield client, events, tools
    finally:
        collector.cancel()
        await pubsub.aclose()
        await listener.aclose()
        await close_redis()


async def _login(client: AsyncClient) -> None:
    creds = {"email": f"{os.urandom(6).hex()}@example.com", "password": "testpass123"}
    assert (await client.post("/auth/register", json=creds)).status_code == 201
    token = (await client.post("/auth/login", data={"username": creds["email"], "password": creds["password"]})).json()
    client.headers["Authorization"] = f"Bearer {token['access_token']}"


async def _create_trip(client: AsyncClient, budget: float = 50_000) -> str:
    body = {
        "destination": "Goa",
        "start_date": str(START),
        "end_date": str(END),
        "budget": budget,
        "group_size": 2,
        "interests": ["history"],
    }
    resp = await client.post("/trips", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _until(condition, what: str, timeout: float = 20.0):
    """Poll an async condition until it returns something truthy."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if result := await condition():
            return result
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


async def _run_finished(client: AsyncClient, trip_id: str, orchestrator_rows: int) -> list[dict]:
    """Wait for the run's closing "orchestrator" row (it is written last) and return all runs."""

    async def done():
        runs = (await client.get(f"/trips/{trip_id}/runs")).json()
        return runs if sum(r["agent_name"] == "orchestrator" for r in runs) >= orchestrator_rows else None

    return await _until(done, f"run {orchestrator_rows} of trip {trip_id}")


@pytest.mark.asyncio
async def test_plan_then_refine_end_to_end(db_session):
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, events, tools):
        await _login(client)

        # Phase 16: saved preferences are injected without being asked for
        prefs = {"dietary_restrictions": ["vegetarian"], "home_city": "Mumbai"}
        assert (await client.put("/users/preferences", json=prefs)).status_code == 200

        trip_id = await _create_trip(client)
        assert (await client.get(f"/trips/{trip_id}/itinerary")).status_code == 404

        resp = await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "A relaxed long weekend in Goa"})
        assert resp.status_code == 202 and resp.json()["status"] == "planning_started"
        # one run at a time
        assert (await client.post(f"/trips/{trip_id}/plan")).status_code == 409

        runs = await _run_finished(client, trip_id, orchestrator_rows=1)

        # ── Phases 9 + 13: every decision node logged, in order, with a duration ──
        names = [r["agent_name"] for r in runs]
        assert names[:4] == ["intent_parsing", "preferences", "flight_agent", "budget_decision"]
        assert set(names[4:6]) == {"hotel_agent", "activities_agent"}  # fanned out concurrently
        assert names[6:] == ["itinerary_builder", "evaluator", "persist", "preference_extractor", "orchestrator"]
        assert all(r["duration_ms"] is not None and r["turn"] == 1 for r in runs)
        assert all(r["status"] == "completed" for r in runs)

        # ── Phase 16: preferences reached the tools ──
        assert tools["flight"].await_args.args[1]["origin"] == "BOM"
        assert "vegetarian" in tools["activities"].await_args.args[1]["interests"]
        learned = (await client.get("/users/preferences")).json()
        assert learned["travel_style"] == "mid-range" and learned["home_city"] == "Mumbai"

        # ── Phases 12 + 13: itinerary persisted, trip completed ──
        trip = (await client.get(f"/trips/{trip_id}")).json()
        assert trip["status"] == "completed"
        assert [t["id"] for t in (await client.get("/trips?status=completed")).json()] == [trip_id]
        first = (await client.get(f"/trips/{trip_id}/itinerary")).json()
        assert first["total_cost"] == 8_200 + 2 * 4_500
        assert [d["hotel"]["name"] for d in first["structured_data"]["days"]] == ["Goa Grand", "Goa Grand"]

        # ── Phase 17: status endpoint + SSE event order ──
        status = (await client.get(f"/trips/{trip_id}/status")).json()
        assert status["status"] == "completed" and status["progress"]["agents_done"] == 3
        seen = [e.get("event") or e["agent"] for e in events]
        assert seen[:2] == ["planning_started", "flight_agent"]
        assert set(seen[2:4]) == {"hotel_agent", "activities_agent"} and seen[4] == "planning_complete"

        # ── Phase 14: two 1536-dim embeddings, written in the background ──
        async def embedded():
            rows = (await db_session.execute(select(Embedding))).scalars().all()
            return rows if len(rows) == 2 else None

        rows = await _until(embedded, "embeddings")
        assert all(r.embedding_model == "gemini-embedding-001" and len(r.vector) == 1536 for r in rows)
        assert (await client.get("/admin/embedding-health")).json()["status"] == "ok"

        # ── Phase 13: timeline is one ordered log of runs + itinerary saves ──
        timeline = (await client.get(f"/trips/{trip_id}/timeline")).json()
        assert [e["timestamp"] for e in timeline] == sorted(e["timestamp"] for e in timeline)
        assert sum(e["event_type"] == "itinerary_saved" for e in timeline) == 1

        # ── Phase 15: "closer to the beach" → only HotelAgent re-runs ──
        tools["hotel"].side_effect = lambda *_: BEACH_HOTELS
        flight_calls = tools["flight"].await_count
        classification = RefinementClassification(refinement_type="targeted_hotel", reason="hotel change")
        with patch("app.api.routes.trips.classify_refinement", AsyncMock(return_value=classification)):
            resp = await client.post(
                f"/trips/{trip_id}/refine", json={"message": "Change hotels to something closer to the beach"}
            )
        assert resp.json()["turn"] == 2 and resp.json()["refinement_type"] == "targeted_hotel"

        await _run_finished(client, trip_id, orchestrator_rows=2)
        turn_2 = [r["agent_name"] for r in (await client.get(f"/trips/{trip_id}/runs?turn=2")).json()]
        assert turn_2 == ["hotel_agent", "itinerary_builder", "evaluator", "persist", "orchestrator"]
        assert tools["flight"].await_count == flight_calls  # flights carried forward, not re-searched

        versions = (await client.get(f"/trips/{trip_id}/itineraries")).json()
        assert len(versions) == 2 and versions[1]["id"] == first["id"]  # newest first, old one kept
        assert versions[0]["structured_data"]["days"][0]["hotel"]["name"] == "Beach House"
        assert versions[0]["total_cost"] == 8_200 + 2 * 6_000
        assert (await client.get(f"/trips/{trip_id}")).json()["status"] == "completed"
        assert [e.get("turn") for e in events if e.get("event") == "planning_complete"] == [1, 2]

        # a second refinement builds on turn 2's state, not turn 1's
        with patch(
            "app.api.routes.trips.classify_refinement",
            AsyncMock(return_value=RefinementClassification(refinement_type="add_day", reason="longer")),
        ):
            resp = await client.post(f"/trips/{trip_id}/refine", json={"message": "add a day"})
        assert resp.json()["turn"] == 3
        await _run_finished(client, trip_id, orchestrator_rows=3)
        assert (await client.get(f"/trips/{trip_id}")).json()["end_date"] == str(END + timedelta(days=1))
        assert len((await client.get(f"/trips/{trip_id}/itineraries")).json()) == 3


@pytest.mark.asyncio
async def test_budget_conflict_then_replan_end_to_end(db_session):
    """Phase 10: ₹40,000 budget, ₹28,000 direct flights → escalate; "cheaper_flights" → completes."""

    def flight_tool(_name: str, params: dict):
        return [flight(15_000.0 if params.get("max_stops", 1) >= 2 else 28_000.0)]

    async with _stack(flight_tool) as (client, events, tools):
        await _login(client)
        trip_id = await _create_trip(client, budget=40_000)

        assert (await client.post(f"/trips/{trip_id}/plan")).status_code == 202
        runs = await _run_finished(client, trip_id, orchestrator_rows=1)

        decision = next(r for r in runs if r["agent_name"] == "budget_decision")
        assert decision["output"]["decision"] == "escalate" and decision["output"]["remaining_budget"] == 12_000
        assert (await client.get(f"/trips/{trip_id}")).json()["status"] == "failed"
        tools["hotel"].assert_not_awaited()  # no point searching hotels the budget cannot cover

        seen = [e.get("event") or e["agent"] for e in events]
        assert seen == ["planning_started", "flight_agent", "budget_conflict", "planning_failed"]
        conflict = events[2]
        assert [o["choice"] for o in conflict["options"]] == ["cheaper_flights", "reduce_days", "increase_budget"]
        # the same options are still there for a client that reloads the page and missed the event
        status = (await client.get(f"/trips/{trip_id}/status")).json()
        assert status["budget_conflict"] == {"reason": conflict["reason"], "options": conflict["options"]}

        # a failed trip has nothing to refine
        with patch("app.api.routes.trips.classify_refinement", AsyncMock()):
            assert (await client.post(f"/trips/{trip_id}/refine", json={"message": "cheaper"})).status_code == 409

        resp = await client.post(f"/trips/{trip_id}/replan", json={"choice": "cheaper_flights"})
        assert resp.json()["status"] == "replanning_started"
        runs = await _run_finished(client, trip_id, orchestrator_rows=2)

        assert tools["flight"].await_args.args[1]["budget"] == 40_000 * 0.65  # tighter cap on the re-plan
        assert [r["output"]["decision"] for r in runs if r["agent_name"] == "budget_decision"] == [
            "escalate",
            "continue",
        ]
        assert (await client.get(f"/trips/{trip_id}")).json()["status"] == "completed"
        assert (await client.get(f"/trips/{trip_id}/status")).json()["budget_conflict"] is None
        assert (await client.get(f"/trips/{trip_id}/itinerary")).json()["total_cost"] == 15_000 + 2 * 4_500


@pytest.mark.asyncio
async def test_plan_survives_missing_flight_and_hotel_providers(db_session):
    """No flight or hotel provider configured → the trip still completes, with activities only."""
    unavailable = {"error": "not configured", "code": "API_NOT_CONFIGURED"}

    def failing_tool(*_):
        from src.ai.mcp_server.models import ToolError

        return ToolError(**unavailable)

    async with _stack(flight_tool=failing_tool, hotel_tool=failing_tool) as (client, events, _):
        await _login(client)
        trip_id = await _create_trip(client)
        await client.post(f"/trips/{trip_id}/plan")
        runs = await _run_finished(client, trip_id, orchestrator_rows=1)

        statuses = {r["agent_name"]: r["status"] for r in runs}
        assert statuses["flight_agent"] == statuses["hotel_agent"] == "failed"
        assert statuses["activities_agent"] == statuses["evaluator"] == "completed"

        itinerary = (await client.get(f"/trips/{trip_id}/itinerary")).json()
        assert itinerary["total_cost"] == 0 and itinerary["structured_data"]["days"][0]["hotel"] is None
        assert (await client.get(f"/trips/{trip_id}/status")).json()["progress"]["agents_done"] == 1
        assert {e["agent"]: e["status"] for e in events if "agent" in e and "event" not in e} == {
            "flight_agent": "failed",
            "hotel_agent": "failed",
            "activities_agent": "completed",
        }


@pytest.mark.asyncio
async def test_restart_recovery_unsticks_interrupted_runs(db_session):
    """A run cut off by a restart must not leave its trip "planning" (every later call would be a 409).

    A first plan that was interrupted has nothing to show → failed. An interrupted
    refinement leaves the previous itinerary standing → completed.
    """
    from app.main import _recover_after_restart
    from app.models.itinerary import Itinerary
    from app.models.trip import Trip, TripStatus
    from app.models.user import User

    user = User(email=f"{os.urandom(6).hex()}@example.com", hashed_password="x")
    db_session.add(user)
    await db_session.commit()

    def trip(status: TripStatus) -> Trip:
        return Trip(user_id=user.id, destination="Goa", start_date=START, end_date=END, budget=50_000, status=status)

    first_plan, refinement, untouched = trip(TripStatus.PLANNING), trip(TripStatus.PLANNING), trip(TripStatus.PENDING)
    db_session.add_all([first_plan, refinement, untouched])
    await db_session.commit()
    db_session.add(Itinerary(trip_id=refinement.id, structured_data={"days": []}, total_cost=0.0))
    await db_session.commit()

    await _recover_after_restart()

    for row in (first_plan, refinement, untouched):
        await db_session.refresh(row)
    assert (first_plan.status, refinement.status, untouched.status) == (
        TripStatus.FAILED,
        TripStatus.COMPLETED,
        TripStatus.PENDING,
    )

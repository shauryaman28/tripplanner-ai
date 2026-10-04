"""End-to-end backend test — Phases 5–20 through the public HTTP API.

Real Postgres (Alembic schema), real Redis, the real LangGraph orchestrator,
real background tasks. Only the external network seams are stubbed: the MCP
tool calls, the builder / intent LLMs, the embedding call and the map tiles
behind the PDF export.

    RUN_INTEGRATION=1 pytest tests/integration/test_pipeline_integration.py -v
"""

import asyncio
import hashlib
import io
import json
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from pypdf import PdfReader
from sqlmodel import select

from app.core.config import settings
from app.models.embedding import Embedding
from src.ai.agents.refinement_classifier import RefinementClassification
from tests.fakes import STUB_TILE_URL, flight, network_stubs

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


async def _create_trip(client: AsyncClient, budget: float = 50_000, destination: str = "Goa") -> str:
    body = {
        "destination": destination,
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


def _pdf_pages(pdf: bytes) -> list[str]:
    """The text of each page of an exported PDF, whitespace collapsed."""
    return [" ".join(page.extract_text().split()) for page in PdfReader(io.BytesIO(pdf)).pages]


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
        assert (await client.get(f"/trips/{trip_id}/export/pdf")).status_code == 404  # nothing to export yet

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
        # one entry per day of the trip; the last day is the departure, with no hotel night
        assert [(d["hotel"] or {}).get("name") for d in first["structured_data"]["days"]] == [
            "Goa Grand",
            "Goa Grand",
            None,
        ]

        # ── Phase 17: status endpoint + SSE event order ──
        status = (await client.get(f"/trips/{trip_id}/status")).json()
        assert status["status"] == "completed" and status["progress"]["agents_done"] == 3
        assert status["progress"]["errors"] == {} and status["run"] is None
        seen = [e.get("event") or e["agent"] for e in events]
        assert seen[:2] == ["planning_started", "flight_agent"]
        assert set(seen[2:4]) == {"hotel_agent", "activities_agent"} and seen[-1] == "planning_complete"

        # ── Phase 20: between the searches and "complete", the itinerary arrives as it is written ──
        tokens = events[4:-1]
        assert tokens and all(e["event"] == "builder_token" and e["agent"] == "itinerary_builder" for e in tokens)
        assert [e["seq"] for e in tokens] == list(range(len(tokens)))
        written = json.loads("".join(e["token"] for e in tokens))
        saved = first["structured_data"]["days"]
        assert [d["morning"]["activity"] for d in written["days"]] == [d["morning"]["activity"] for d in saved]
        assert written["total_cost"] == first["total_cost"]
        # what was streamed is the model's text; what was saved went through the checks (coordinates attached)
        assert written["days"][0]["morning"]["lat"] is None and saved[0]["morning"]["lat"] == 15.492

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

        # ── Phase 19: the saved itinerary downloads as a PDF — cover, days, costs, map ──
        cache = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        tile_keys = f"maptile:{hashlib.sha1(STUB_TILE_URL.encode()).hexdigest()[:8]}:*"
        for key in await cache.keys(tile_keys):  # an earlier run's tiles would hide a cache that never fills
            await cache.delete(key)

        exported = await client.get(f"/trips/{trip_id}/export/pdf")
        assert exported.status_code == 200 and exported.headers["content-type"] == "application/pdf"
        assert exported.headers["content-disposition"] == f'attachment; filename="trip-goa-{START}.pdf"'
        assert exported.headers["x-itinerary-map"] == "included"
        cover, days, costs, on_the_map = _pdf_pages(exported.content)
        assert "Goa" in cover and "ESTIMATED TOTAL ₹17,200" in cover and "2 travellers" in cover
        assert "Fort Aguada" in days and "STAY Goa Grand" in days and "FLIGHT DEL → GOI" in days
        assert "Flights DEL → GOI · return · 2 travellers ₹8,200" in costs and "Estimated total ₹17,200" in costs
        assert "Fort Aguada — Day 1, morning" in on_the_map
        assert [len(page.images) for page in PdfReader(io.BytesIO(exported.content)).pages] == [0, 0, 0, 1]

        # the map's tiles are in Redis for a week, under the stub tile server's keys — never the real one's
        cached = await cache.keys(tile_keys)
        assert cached and await cache.ttl(cached[0]) > 6 * 24 * 3600
        real_provider = hashlib.sha1(type(settings).model_fields["MAP_TILE_URL"].default.encode()).hexdigest()[:8]
        assert tile_keys.split(":")[1] != real_provider
        again = await client.get(f"/trips/{trip_id}/export/pdf")
        assert again.status_code == 200 and sorted(await cache.keys(tile_keys)) == sorted(cached)
        await cache.aclose()

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
        # the refinement's build is a stream of its own: it starts again from seq 0
        assert [e["seq"] for e in events if e.get("event") == "builder_token"].count(0) == 2

        # Phase 19: the export is of the latest version — the new hotel, the new total
        refined = " ".join(_pdf_pages((await client.get(f"/trips/{trip_id}/export/pdf")).content))
        assert "Beach House" in refined and "Goa Grand" not in refined and "ESTIMATED TOTAL ₹20,200" in refined

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
async def test_pdf_export_is_private_and_survives_a_dead_tile_server(db_session):
    """Phase 19: only the owner can export a trip; a map that cannot be drawn is left out, not fatal."""
    from app.core.security import create_access_token

    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, _, __):
        await _login(client)
        trip_id = await _create_trip(client)
        await client.post(f"/trips/{trip_id}/plan")
        await _run_finished(client, trip_id, orchestrator_rows=1)
        owner = client.headers["Authorization"]

        client.headers.pop("Authorization")
        assert (await client.get(f"/trips/{trip_id}/export/pdf")).status_code == 401
        await _login(client)  # somebody else
        assert (await client.get(f"/trips/{trip_id}/export/pdf")).status_code == 404
        client.headers["Authorization"] = f"Bearer {create_access_token('not-a-user-id')}"
        assert (await client.get(f"/trips/{trip_id}/export/pdf")).status_code == 401

        client.headers["Authorization"] = owner
        with (
            patch("app.pdf.static_map._download_tile", AsyncMock(side_effect=OSError("tile server is down"))),
            patch.object(settings, "MAP_TILE_URL", "https://tiles.down.invalid/{z}/{x}/{y}.png"),  # nothing cached
        ):
            exported = await client.get(f"/trips/{trip_id}/export/pdf")
        assert exported.status_code == 200 and exported.headers["x-itinerary-map"] == "unavailable"
        pages = _pdf_pages(exported.content)
        assert len(pages) == 3 and "Cost breakdown" in pages[2] and "On the map" not in " ".join(pages)


@pytest.mark.asyncio
async def test_a_trip_typed_in_devanagari_exports_in_devanagari(db_session):
    """Phase 20: the destination is drawn in its own script, and names the file in it."""
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, _, __):
        await _login(client)
        trip_id = await _create_trip(client, destination="गोवा")
        assert (await client.post(f"/trips/{trip_id}/plan")).status_code == 202
        await _run_finished(client, trip_id, orchestrator_rows=1)

        exported = await client.get(f"/trips/{trip_id}/export/pdf")
        assert exported.status_code == 200
        assert exported.headers["content-disposition"] == (
            f'attachment; filename="trip-{START}.pdf"; '
            f"filename*=UTF-8''trip-%E0%A4%97%E0%A5%8B%E0%A4%B5%E0%A4%BE-{START}.pdf"
        )
        reader = PdfReader(io.BytesIO(exported.content))
        fonts = {
            str(ref.get_object()["/BaseFont"]).split("+")[-1] for ref in reader.pages[0]["/Resources"]["/Font"].values()
        }
        assert "NotoSansDevanagari-Medium" in fonts  # the cover's title
        assert all(page.startswith("गोवा · ") for page in _pdf_pages(exported.content)[1:])  # the running head


@pytest.mark.asyncio
def _dear_flights(_name: str, params: dict):
    """₹30,000 direct, ₹15,000 with connections: on ₹40,000 for two over three days, a conflict in any month.

    (At ₹28,000 the trip as asked comes to exactly ₹40,000 at off-season prices — a trip that fits is not
    stopped since Phase 21 — and the test would pass or fail with the month it runs in.)
    """
    return [flight(15_000.0 if params.get("max_stops", 1) >= 2 else 30_000.0)]


@pytest.mark.asyncio
async def test_budget_conflict_then_replan_end_to_end(db_session):
    """Phase 10: ₹40,000 budget, ₹30,000 direct flights → escalate; "cheaper_flights" → completes.

    Phase 21: the conflict prices the trip as asked and three ways out, with nothing searched for them.
    """
    flight_tool = _dear_flights

    async with _stack(flight_tool) as (client, events, tools):
        await _login(client)
        trip_id = await _create_trip(client, budget=40_000)

        assert (await client.post(f"/trips/{trip_id}/plan")).status_code == 202
        runs = await _run_finished(client, trip_id, orchestrator_rows=1)

        decision = next(r for r in runs if r["agent_name"] == "budget_decision")
        assert decision["output"]["decision"] == "escalate" and decision["output"]["remaining_budget"] == 10_000
        assert (await client.get(f"/trips/{trip_id}")).json()["status"] == "failed"
        tools["hotel"].assert_not_awaited()  # no point searching hotels the budget cannot cover
        tools["activities"].assert_not_awaited()  # …and the ways out below were priced without a search

        seen = [e.get("event") or e["agent"] for e in events]
        assert seen == ["planning_started", "flight_agent", "budget_conflict", "planning_failed"]
        conflict = events[2]
        choices = [o["choice"] for o in conflict["options"]]
        assert choices[:2] == ["cheaper_hotel", "reduce_days"] and choices[-2:] == [
            "cheaper_flights",
            "increase_budget",
        ]
        priced = [o for o in conflict["options"] if o["choice"] in ("cheaper_hotel", "reduce_days", "off_peak")]
        assert all(isinstance(o["total"], int) and o["total"] % 100 == 0 for o in priced)  # whole rupees, said round
        estimate = conflict["estimate"]
        assert estimate["total"] > 40_000 and estimate["total_min"] < estimate["total"] < estimate["total_max"]
        # the same options are still there for a client that reloads the page and missed the event
        status = (await client.get(f"/trips/{trip_id}/status")).json()
        assert status["budget_conflict"] == {
            "reason": conflict["reason"],
            "options": conflict["options"],
            "estimate": estimate,
        }

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
async def test_a_shorter_trip_picked_in_a_conflict_goes_ahead_on_the_same_flights(db_session):
    """Phase 21: the flights are still ₹30,000 of ₹40,000 — the check goes ahead with the traveller's choice."""
    async with _stack(_dear_flights) as (client, events, _):
        await _login(client)
        trip_id = await _create_trip(client, budget=40_000)
        await client.post(f"/trips/{trip_id}/plan")
        await _run_finished(client, trip_id, orchestrator_rows=1)
        offered = next(e for e in events if e.get("event") == "budget_conflict")["options"]
        shorter = next(o for o in offered if o["choice"] == "reduce_days")
        assert shorter["days"] == 2 and shorter["description"] == "Make it 2 days instead of 3"

        resp = await client.post(f"/trips/{trip_id}/replan", json={"choice": "reduce_days"})
        assert resp.json() == {"status": "replanning_started", "trip_id": trip_id, "choice": "reduce_days"}
        runs = await _run_finished(client, trip_id, orchestrator_rows=2)

        checks = [r["output"] for r in runs if r["agent_name"] == "budget_decision"]
        assert [check["decision"] for check in checks] == ["escalate", "continue"]
        assert checks[1]["flight_cost"] == 30_000  # the same flights…
        assert (
            checks[1]["reason"]
            == "Flights cost ₹30,000 — 75% of the budget — going ahead with the shorter trip you chose."
        )
        trip = (await client.get(f"/trips/{trip_id}")).json()
        assert (trip["status"], trip["end_date"]) == ("completed", str(START + timedelta(days=1)))
        days = (await client.get(f"/trips/{trip_id}/itinerary")).json()["structured_data"]["days"]
        assert len(days) == 2
        assert (await client.get(f"/trips/{trip_id}/status")).json()["budget_conflict"] is None


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
async def test_a_failed_search_can_be_retried_and_the_plan_keeps_the_rest(db_session):
    """Phase 20: the flight provider is down → a plan without flights → retry → the same plan, with them."""
    from src.ai.mcp_server.models import ToolError

    down = ToolError(error="The flight search is not answering (HTTP 503).", code="PROVIDER_ERROR")
    answers = [down, down, [flight(8_200.0)]]  # the first run, a retry that fails again, a retry that works

    async with _stack(flight_tool=lambda *_: answers.pop(0)) as (client, events, tools):
        await _login(client)
        trip_id = await _create_trip(client)
        await client.post(f"/trips/{trip_id}/plan")
        await _run_finished(client, trip_id, orchestrator_rows=1)

        first = (await client.get(f"/trips/{trip_id}/itinerary")).json()
        assert first["total_cost"] == 2 * 4_500 and first["structured_data"]["days"][0]["flight"] is None
        status = (await client.get(f"/trips/{trip_id}/status")).json()
        assert status["status"] == "completed" and status["progress"]["agents"]["flight_agent"] == "failed"
        assert status["progress"]["errors"] == {"flight_agent": down.error}  # which search failed, and why

        # ── a retry that fails again: nothing is rebuilt, the plan stands, the reason is said ──
        resp = await client.post(f"/trips/{trip_id}/retry", json={"agent": "flight_agent"})
        assert resp.status_code == 200 and resp.json()["status"] == "retry_started" and resp.json()["turn"] == 2
        assert (await client.post(f"/trips/{trip_id}/retry", json={"agent": "flight_agent"})).status_code == 409
        await _run_finished(client, trip_id, orchestrator_rows=2)

        turn_2 = [(r["agent_name"], r["status"]) for r in (await client.get(f"/trips/{trip_id}/runs?turn=2")).json()]
        assert turn_2 == [("flight_agent", "failed"), ("orchestrator", "failed")]
        assert events[-1] == {
            "event": "planning_failed",
            "agent": "flight_agent",
            "status": "failed",
            "error": down.error,
        }
        assert (await client.get(f"/trips/{trip_id}")).json()["status"] == "completed"
        assert len((await client.get(f"/trips/{trip_id}/itineraries")).json()) == 1
        assert (await client.get(f"/trips/{trip_id}/status")).json()["run"] is None

        # ── a retry that works: only the flight search runs again, and the plan gains the flight ──
        hotel_calls, activity_calls = tools["hotel"].await_count, tools["activities"].await_count
        resp = await client.post(f"/trips/{trip_id}/retry", json={"agent": "flight_agent"})
        assert resp.json()["turn"] == 3 and resp.json()["refinement_type"] == "targeted_flights"
        await _run_finished(client, trip_id, orchestrator_rows=3)

        turn_3 = [r["agent_name"] for r in (await client.get(f"/trips/{trip_id}/runs?turn=3")).json()]
        assert turn_3 == [
            "flight_agent",
            "budget_decision",
            "itinerary_builder",
            "evaluator",
            "persist",
            "orchestrator",
        ]
        assert (tools["hotel"].await_count, tools["activities"].await_count) == (hotel_calls, activity_calls)

        retried = (await client.get(f"/trips/{trip_id}/itinerary")).json()
        assert retried["id"] != first["id"] and retried["total_cost"] == 8_200 + 2 * 4_500
        before, after = first["structured_data"]["days"], retried["structured_data"]["days"]
        assert after[0]["flight"]["price_inr"] == 8_200
        for slot in ("morning", "afternoon", "evening", "hotel"):  # everything else is where it was
            assert [d[slot] for d in after] == [d[slot] for d in before]
        status = (await client.get(f"/trips/{trip_id}/status")).json()
        assert status["progress"]["errors"] == {} and status["progress"]["agents_done"] == 3
        started = [e for e in events if e.get("event") == "planning_started"]
        assert [(e["turn"], e.get("retry")) for e in started] == [(1, None), (2, "flight_agent"), (3, "flight_agent")]


@pytest.mark.asyncio
async def test_a_trip_that_failed_can_be_retried_whole(db_session):
    """Phase 20: every provider down → the trip fails → retry plans it again, from the same request."""
    from src.ai.mcp_server.models import ToolError

    down = ToolError(error="The provider is not answering (HTTP 503).", code="PROVIDER_ERROR")
    up = {"flights": False, "hotels": False}

    async with _stack(
        flight_tool=lambda *_: [flight(8_200.0)] if up["flights"] else down,
        hotel_tool=lambda *_: BEACH_HOTELS if up["hotels"] else down,
    ) as (client, events, tools):
        attractions = tools["activities"].return_value
        tools["activities"].return_value = down
        await _login(client)
        trip_id = await _create_trip(client)
        await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and quiet beaches"})
        await _run_finished(client, trip_id, orchestrator_rows=1)

        status = (await client.get(f"/trips/{trip_id}/status")).json()
        assert status["status"] == "failed" and set(status["progress"]["errors"]) == set(status["progress"]["agents"])
        assert "nothing to build a plan from" in status["failure_reason"]

        up.update(flights=True, hotels=True)
        tools["activities"].return_value = attractions
        # a search is named, but there is no plan to add it to: the whole trip is planned again
        resp = await client.post(f"/trips/{trip_id}/retry", json={"agent": "hotel_agent"})
        assert resp.status_code == 200 and resp.json() == {"status": "planning_started", "trip_id": trip_id}
        runs = await _run_finished(client, trip_id, orchestrator_rows=2)

        assert (await client.get(f"/trips/{trip_id}")).json()["status"] == "completed"
        assert (await client.get(f"/trips/{trip_id}/itinerary")).json()["total_cost"] == 8_200 + 2 * 6_000
        # it was planned from what the traveller had written, not from nothing
        parsing = [r for r in runs if r["agent_name"] == "intent_parsing"]
        assert [r["input"]["raw_input"] for r in parsing] == ["forts and quiet beaches"] * 2
        status = (await client.get(f"/trips/{trip_id}/status")).json()
        assert status["failure_reason"] is None and status["progress"]["errors"] == {}


@pytest.mark.asyncio
async def test_a_destination_the_planner_cannot_serve_fails_with_the_reason(db_session):
    """ "London": no airport, and the place is abroad → the run stops and says so; nothing is saved as a plan."""
    from src.ai.mcp_server.models import ToolError

    abroad = ToolError(
        error="London is in United Kingdom. This planner covers trips within India for now.", code="OUTSIDE_COVERAGE"
    )
    no_airport = ToolError(error="Unknown airport: 'London'.", code="UNKNOWN_DESTINATION")

    async with _stack(flight_tool=lambda *_: no_airport, hotel_tool=lambda *_: abroad) as (client, events, tools):
        tools["activities"].return_value = abroad
        await _login(client)
        trip_id = await _create_trip(client)
        await client.post(f"/trips/{trip_id}/plan")
        runs = await _run_finished(client, trip_id, orchestrator_rows=1)

        names = [r["agent_name"] for r in runs]
        assert "nothing_found" in names and "itinerary_builder" not in names  # no build-and-retry over nothing
        assert (await client.get(f"/trips/{trip_id}")).json()["status"] == "failed"
        assert (await client.get(f"/trips/{trip_id}/itinerary")).status_code == 404
        assert events[-1]["event"] == "planning_failed" and events[-1]["error"] == abroad.error
        # ...and a page loaded later can still say why
        assert (await client.get(f"/trips/{trip_id}/status")).json()["failure_reason"] == abroad.error


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

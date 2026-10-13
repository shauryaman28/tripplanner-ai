"""Phase 31 — the tool server is killed, for real, under the real client.

Real Redis, and the tool server as it runs in production: `python -m src.ai.mcp_server.server`, a
process of its own on the other end of two pipes. docs/chaos-report.md found that killing it took
every later search with it until the backend was restarted; the unit tests hold the client's
decisions, and this holds the thing itself — a process that is sent SIGKILL, and what the next call,
the calls in flight, and the task that happens to be making them then see.

    RUN_INTEGRATION=1 pytest tests/integration/test_phase31_chaos_integration.py -v
"""

import asyncio
import os
import signal
import subprocess
import time
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from prometheus_client import REGISTRY

from app.api.routes import trips
from app.db import redis as redis_db
from src.ai.agents.refinement_classifier import RefinementClassification
from src.ai.builder import builder
from src.ai.mcp_client import client as mcp_client
from src.ai.mcp_client.client import call_tool, close_session
from src.ai.mcp_server import cache as tool_cache
from src.ai.mcp_server.models import ToolError
from src.ai.orchestrator import orchestrator
from src.ai.utils.conversation import _hist_key, _state_key
from tests.fakes import ATTRACTIONS, HOTELS, FakeProviders, flight, tool_server
from tests.integration.test_pipeline_integration import _create_trip, _login, _run_finished, _stack, _until

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)


def tool_servers() -> list[int]:
    """The tool server processes this process has started."""
    found = subprocess.run(
        ["pgrep", "-P", str(os.getpid()), "-f", "src.ai.mcp_server.server"], capture_output=True, text=True
    )
    return [int(pid) for pid in found.stdout.split()]


def kill_them() -> list[int]:
    killed = tool_servers()
    for pid in killed:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return killed


def starts() -> float:
    return REGISTRY.get_sample_value("mcp_tool_server_starts_total")


@pytest.fixture
async def a_search_the_real_process_can_answer(monkeypatch):
    """A flight search whose answer is in Redis already, and a process that can reach nothing else."""
    day = date.today() + timedelta(days=47)
    search = {
        "origin": "DEL",
        "destination": "GOI",
        "date": str(day),
        "return_date": str(day + timedelta(days=3)),
        "budget": 50_000.0,
    }
    tool_cache.reset_client()
    async with tool_server(FakeProviders()):
        seeded = await call_tool("search_flights", search)
    assert not isinstance(seeded, ToolError) and seeded

    await close_session()  # whatever an earlier test left: the one below is started with this environment
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "not-a-key")
    for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(proxy, "http://127.0.0.1:9")  # every way out of the process is shut
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    yield search, seeded
    await close_session()
    kill_them()


async def test_a_killed_tool_server_is_replaced_and_the_search_that_found_it_dead_is_answered(
    a_search_the_real_process_can_answer,
):
    search, seeded = a_search_the_real_process_can_answer
    before = starts()

    assert await call_tool("search_flights", search) == seeded  # starts the tool server
    (first,) = tool_servers()
    assert starts() - before == 1

    assert kill_them() == [first]
    await asyncio.sleep(0.2)  # the pipe closes; nobody has called since
    assert tool_servers() == []  # …and nothing starts one by itself: the next call does

    answered = await call_tool("search_flights", search)
    assert answered == seeded, answered  # not an error and not half an answer: the search, asked again of a new server
    (second,) = tool_servers()
    assert second != first and starts() - before == 2

    await close_session()
    await asyncio.sleep(0.3)
    assert tool_servers() == []  # closing puts the new one away as it did the first: no process is left behind
    assert mcp_client._session is None and mcp_client._client_context is None


async def test_searches_in_flight_when_it_dies_are_all_answered_and_nobody_is_cancelled(
    a_search_the_real_process_can_answer,
):
    """Three searches side by side, as a plan makes them — and the task making them is a planning run.

    Putting a dead server's pipes away from the task that found them cancelled the task that had
    opened them: whichever request had needed a search first. The run is the one that must survive.
    """
    search, seeded = a_search_the_real_process_can_answer
    assert await call_tool("search_flights", search) == seeded
    before, cancelled = starts(), False

    async def a_run() -> list:
        nonlocal cancelled
        try:
            results = []
            for _ in range(6):  # for a little while, so that one round at least is in flight at the kill
                results += await asyncio.gather(*(call_tool("search_flights", search) for _ in range(3)))
                await asyncio.sleep(0.05)
            return results
        except asyncio.CancelledError:
            cancelled = True
            raise

    run = asyncio.ensure_future(a_run())
    await asyncio.sleep(0.08)
    assert kill_them()
    results = await asyncio.wait_for(run, 60)

    assert not cancelled
    assert len(results) == 18 and all(result == seeded for result in results), [
        result for result in results if result != seeded
    ][:2]
    assert starts() - before == 1  # one new server for all of them, not one each


async def test_a_tool_server_that_keeps_dying_is_given_up_on_by_name_and_nothing_hangs(
    a_search_the_real_process_can_answer,
):
    search, seeded = a_search_the_real_process_can_answer
    assert await call_tool("search_flights", search) == seeded
    stop = asyncio.Event()

    async def keep_it_dead() -> None:
        while not stop.is_set():
            kill_them()
            await asyncio.sleep(0.03)

    killing = asyncio.ensure_future(keep_it_dead())
    began = time.monotonic()
    try:
        told = await asyncio.wait_for(
            asyncio.gather(*(call_tool("search_flights", search) for _ in range(3))), 60
        )  # a call sent a moment after the pipe closed used to wait for ever
    finally:
        stop.set()
        await killing

    assert [(type(t).__name__, getattr(t, "code", None)) for t in told] == [("ToolError", "CONNECTION_REFUSED")] * 3
    assert all(t.error == "The tool server could not be reached." for t in told)
    assert time.monotonic() - began < 45

    # …and once it is left alone, the very next call has a tool server again
    assert await call_tool("search_flights", search) == seeded
    assert len(tool_servers()) == 1


# ── A whole plan, with something taken away from under it ───────────────────

GONE = ToolError(error="The tool server could not be reached.", code="CONNECTION_REFUSED")


async def test_a_plan_made_without_a_search_is_saved_saying_so(db_session):
    """The hotel search finds the tool server gone, and stays gone: the plan is made from the rest — and the
    itinerary that is saved, which is what the page, the PDF and every later reader have, says what it lacks."""
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, events, tools):
        tools["hotel"].side_effect = lambda *_: GONE
        tools["activities"].side_effect = lambda *_: ATTRACTIONS
        await _login(client)
        trip_id = await _create_trip(client)
        assert (await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and food"})).status_code == 202
        runs = await _run_finished(client, trip_id, orchestrator_rows=1)

        hotel = next(run for run in runs if run["agent_name"] == "hotel_agent")
        assert hotel["status"] == "failed" and hotel["output"]["error"]["code"] == "CONNECTION_REFUSED"
        status = (await client.get(f"/trips/{trip_id}/status")).json()
        assert status["status"] == "completed" and status["progress"]["agents_done"] == 2
        assert status["progress"]["errors"] == {
            "hotel_agent": "The search service stopped answering. Try again in a moment."
        }
        assert status["progress"]["retryable"] == {"hotel_agent": True}

        saved = (await client.get(f"/trips/{trip_id}/itinerary")).json()["structured_data"]
        assert saved["missing"] == [
            {
                "search": "hotel_agent",
                "what": "a place to stay",
                "why": "The search service stopped answering. Try again in a moment.",
                "retryable": True,
            }
        ]
        assert all(day["hotel"] is None for day in saved["days"])  # and it has no hotel in it: the label is true
        heard = [event for event in events if event.get("agent") == "hotel_agent" and event.get("status") == "failed"]
        assert heard and heard[0]["summary"] == "The search service stopped answering. Try again in a moment."


async def test_with_no_search_able_to_run_the_trip_fails_for_that_reason_and_not_for_its_destination(db_session):
    async with _stack(flight_tool=lambda *_: GONE) as (client, events, tools):
        tools["hotel"].side_effect = lambda *_: GONE
        tools["activities"].side_effect = lambda *_: GONE
        await _login(client)
        trip_id = await _create_trip(client)
        await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and food"})
        await _run_finished(client, trip_id, orchestrator_rows=1)

        status = (await client.get(f"/trips/{trip_id}/status")).json()
        assert status["status"] == "failed" and status["failure_reason"] == orchestrator.SEARCHES_DID_NOT_RUN
        assert (await client.get(f"/trips/{trip_id}/itinerary")).status_code == 404  # nothing was saved

        # …and with the searches back, the same trip is planned
        tools["flight"].side_effect = lambda *_: [flight(8_200.0)]
        tools["hotel"].side_effect = lambda *_: HOTELS
        tools["activities"].side_effect = lambda *_: ATTRACTIONS
        assert (await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and food"})).status_code == 202
        await _run_finished(client, trip_id, orchestrator_rows=2)
        again = (await client.get(f"/trips/{trip_id}/status")).json()
        assert again["status"] == "completed" and again["failure_reason"] is None
        assert (
            "missing" not in (await client.get(f"/trips/{trip_id}/itinerary")).json()["structured_data"]
            or not (await client.get(f"/trips/{trip_id}/itinerary")).json()["structured_data"]["missing"]
        )


async def test_a_builder_whose_model_never_answers_fails_the_trip_at_the_deadline_and_says_why(db_session):
    """Before: the trip said "planning" until the process was restarted. Now: the deadline, one failed row, a reason."""

    async def never(*_args, **_kwargs):
        await asyncio.sleep(3600)

    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, events, tools):
        await _login(client)
        trip_id = await _create_trip(client)
        began = time.monotonic()
        with patch.object(builder, "_call_llm", never), patch.object(builder, "BUILD_DEADLINE_S", 0.3):
            assert (
                await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and food"})
            ).status_code == 202
            assert (await client.post(f"/trips/{trip_id}/plan", json={})).status_code == 409  # one run at a time
            runs = await _run_finished(client, trip_id, orchestrator_rows=1)
        assert time.monotonic() - began < 15

        builds = [run for run in runs if run["agent_name"] == "itinerary_builder"]
        assert (
            len(builds) == 1 and builds[0]["status"] == "failed"
        )  # built once: a model that is silent is not asked again
        assert builds[0]["output"]["error"]["code"] == "LLM_TIMEOUT"
        status = (await client.get(f"/trips/{trip_id}/status")).json()
        said = "The plan could not be written: the model that writes it did not answer in time. Try again in a moment."
        assert status["status"] == "failed" and status["failure_reason"] == said
        failed = [event for event in events if event.get("event") == "planning_failed"]
        assert failed and failed[-1]["error"] == said

        # the model answers again: the same trip is planned, by the same button
        assert (await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and food"})).status_code == 202
        await _run_finished(client, trip_id, orchestrator_rows=2)
        assert (await client.get(f"/trips/{trip_id}/status")).json()["status"] == "completed"


async def test_a_run_that_never_comes_back_is_stopped_and_the_trip_is_not_left_planning(db_session):
    """The backstop: whatever hangs — here, the first step — the run as a whole has an end."""

    async def never(_message: str) -> dict:
        await asyncio.sleep(3600)

    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, events, tools):
        await _login(client)
        trip_id = await _create_trip(client)
        with patch.object(orchestrator, "_extract_intent", never), patch.object(trips, "RUN_DEADLINE_S", 0.4):
            await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and food"})

            async def not_planning():
                return (await client.get(f"/trips/{trip_id}/status")).json()["status"] != "planning"

            await _until(not_planning, "the run to be stopped at its deadline")

        assert (await client.get(f"/trips/{trip_id}/status")).json()["status"] == "failed"
        failed = [event for event in events if event.get("event") == "planning_failed"]
        assert failed and failed[-1]["error"] == trips.TOOK_TOO_LONG
        # and it can be asked for again
        assert (await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and food"})).status_code == 202
        await _run_finished(client, trip_id, orchestrator_rows=1)
        assert (await client.get(f"/trips/{trip_id}/status")).json()["status"] == "completed"


async def test_a_planned_trip_whose_state_redis_has_lost_can_still_be_changed(db_session):
    """Redis killed as a plan ended, or come back empty, or simply a day later: the trip has an itinerary in
    Postgres and "No prior planning state found. Run POST /plan first." was all a change request got."""
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, events, tools):
        await _login(client)
        trip_id = await _create_trip(client)
        await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and food"})
        await _run_finished(client, trip_id, orchestrator_rows=1)
        first = (await client.get(f"/trips/{trip_id}/itinerary")).json()

        await redis_db.redis_client.delete(
            _state_key(trip_id), _hist_key(trip_id)
        )  # what a Redis that came back empty has

        asked_for = RefinementClassification(refinement_type="targeted_hotel", reason="a different hotel")
        with patch("app.api.routes.trips.classify_refinement", AsyncMock(return_value=asked_for)):
            answer = await client.post(f"/trips/{trip_id}/refine", json={"message": "a nicer hotel, please"})
        assert answer.status_code == 200, answer.text
        told = answer.json()
        # the hotels found before are gone with the state: the change is made the way that needs none of them
        assert told["refinement_type"] == "full_replan" and "targeted_hotel" in told["reason"]
        assert told["turn"] == 2  # counted from the rows in Postgres: the conversation went with the state

        runs = await _run_finished(client, trip_id, orchestrator_rows=2)
        assert {run["turn"] for run in runs} == {1, 2}
        second = (await client.get(f"/trips/{trip_id}/itinerary")).json()
        assert second["id"] != first["id"] and second["structured_data"]["days"]
        assert (await client.get(f"/trips/{trip_id}/status")).json()["status"] == "completed"

        # and the run saved its state again: the next change is an ordinary one
        with patch("app.api.routes.trips.classify_refinement", AsyncMock(return_value=asked_for)):
            ordinary = await client.post(f"/trips/{trip_id}/refine", json={"message": "closer to the fort"})
        assert ordinary.status_code == 200 and ordinary.json()["refinement_type"] == "targeted_hotel"
        assert ordinary.json()["turn"] == 3
        await _run_finished(client, trip_id, orchestrator_rows=3)


async def test_a_trip_that_was_never_planned_still_has_nothing_to_change(db_session):
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, events, tools):
        await _login(client)
        trip_id = await _create_trip(client)
        with patch("app.api.routes.trips.classify_refinement", AsyncMock()) as classify:
            refused = await client.post(f"/trips/{trip_id}/refine", json={"message": "cheaper"})
        assert refused.status_code == 409 and "Run POST /plan first" in refused.json()["detail"]
        classify.assert_not_awaited()

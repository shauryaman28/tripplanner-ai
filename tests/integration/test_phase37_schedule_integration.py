"""Phase 37 — the travel-time tool through the real tool server, and a plan's clock through the public API.

    RUN_INTEGRATION=1 pytest tests/integration/test_phase37_schedule_integration.py -v

The tool: the real MCP server and client (tests/fakes.tool_server), in front of a stand-in that answers as
Google's Routes API does, with the real Redis as its cache — the roadmap's "cached on the second call
([CACHE HIT] in logs)". The plan: the real routes, orchestrator, builder and PDF on Postgres and Redis, the
providers stubbed as everywhere (tests/fakes.network_stubs), the travel times estimated — the stubs have no key.
"""

import io
import logging
import os
from unittest.mock import patch

import pytest
from pypdf import PdfReader

from src.ai.builder import builder
from src.ai.itinerary import SLOTS
from src.ai.mcp_client.client import call_tool
from src.ai.mcp_server.models import ToolError
from tests.fakes import FakeProviders, flight, tool_server
from tests.integration.test_phase24_caching_integration import families, tool_cache  # noqa: F401 — the fixture
from tests.integration.test_pipeline_integration import _login, _run_finished, _stack

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

FORT_AGUADA_TO_BAGA = {"origin_lat": 15.4925, "origin_lng": 73.7735, "dest_lat": 15.5553, "dest_lng": 73.7517}


async def test_the_tool_answers_through_the_server_and_the_second_ask_is_a_cache_hit(tool_cache, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger="src.ai.mcp_server.cache")
    providers = FakeProviders()
    async with tool_server(providers):
        first = await call_tool("get_travel_time", {**FORT_AGUADA_TO_BAGA, "mode": "auto-rickshaw"})
        second = await call_tool("get_travel_time", {**FORT_AGUADA_TO_BAGA, "mode": "auto-rickshaw"})
        walking = await call_tool("get_travel_time", {**FORT_AGUADA_TO_BAGA, "mode": "walk"})

    assert first == second == {"minutes": 20, "km": 9.6, "mode": "auto-rickshaw", "source": "google"}
    assert walking["minutes"] > 100 and walking["mode"] == "walk"  # another mode, another journey
    assert [route["travelMode"] for route in providers.routes] == [
        "DRIVE",
        "WALK",
    ]  # Google asked twice, not three times
    (key, _walk) = sorted(families(tool_cache)["travel"], key=lambda k: tool_cache.ttl(k))
    assert 86_000 < tool_cache.ttl(key) <= 86_400  # a day, read back from Redis
    lines = [record.getMessage() for record in caplog.records]
    assert any(line.startswith("[CACHE HIT]  mcp:travel:") for line in lines), lines


async def test_without_a_key_the_tool_says_so_through_the_server(tool_cache):  # noqa: F811
    async with tool_server():
        with patch("src.ai.mcp_server.tools.mcp_settings.GOOGLE_MAPS_API_KEY", ""):
            answer = await call_tool("get_travel_time", {**FORT_AGUADA_TO_BAGA, "mode": "taxi"})
    assert isinstance(answer, ToolError) and answer.code == "API_NOT_CONFIGURED"


async def test_the_builder_asks_the_real_server_and_a_plan_is_saved_with_its_clock(db_session, tool_cache):  # noqa: F811
    """A plan through the API, its builder asking the travel-time tool through the real server (the other tools
    stubbed as everywhere): every stop saved with its times, the ways Google's, the far ones with a note."""
    providers = FakeProviders()
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, _events, _tools):
        async with tool_server(providers):
            with patch.object(builder, "call_tool", call_tool):  # the real client, to the real server
                await _login(client)
                body = {"destination": "Goa", "start_date": "2030-01-10", "end_date": "2030-01-12", "budget": 60_000,
                        "group_size": 1, "interests": ["history"]}  # fmt: skip
                trip_id = (await client.post("/trips", json=body)).json()["id"]
                assert (
                    await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "A long weekend"})
                ).status_code == 202
                runs = await _run_finished(client, trip_id, orchestrator_rows=1)

        plan = (await client.get(f"/trips/{trip_id}/itinerary")).json()["structured_data"]
        stops = [day[slot] for day in plan["days"] for slot in SLOTS if day.get(slot)]
        assert stops and all(stop["start_time"] and stop["end_time"] for stop in stops)
        notes = [stop["travel"] for stop in stops if stop.get("travel")]
        assert notes and all(note["source"] == "google" and note["km"] for note in notes)
        assert {note["from"] for note in notes} >= {"the airport"}  # the stub's flight lands on day 1 at 08:15
        built = next(run for run in runs if run["agent_name"] == "itinerary_builder" and run["status"] == "completed")
        assert built["output"]["travel"]["legs"] == built["output"]["travel"]["from_google"] == len(providers.routes)

        pdf = await client.get(f"/trips/{trip_id}/export/pdf")
        text = " ".join(" ".join(page.extract_text().split()) for page in PdfReader(io.BytesIO(pdf.content)).pages)
        assert f"MORNING · {stops[0]['start_time']}–{stops[0]['end_time']}" in text
        assert "from the airport" in text

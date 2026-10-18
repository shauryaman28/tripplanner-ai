"""Phase 38 — the weather, through the public API and the real tool server, on real Postgres and Redis.

    RUN_INTEGRATION=1 pytest tests/integration/test_phase38_weather_integration.py -v

A plan: the real routes, orchestrator, builder and PDF, the providers stubbed (tests/fakes.network_stubs) — its
stand-in forecast has rain on Alleppey's second day. The tool: the real MCP server and client in front of a
stand-in OpenWeatherMap (tests/fakes.FakeProviders), with Redis as its cache.
"""

import io
import os
from datetime import date, timedelta

import pytest
from pypdf import PdfReader

from src.ai import weather
from src.ai.itinerary import SLOTS
from src.ai.mcp_client.client import call_tool
from tests.fakes import ATTRACTIONS, FakeProviders, flight, tool_server
from tests.integration.test_phase24_caching_integration import families, tool_cache  # noqa: F401 — the fixture
from tests.integration.test_pipeline_integration import _login, _run_finished, _stack

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

MUSEUM = {"name": "Revi Karunakaran Museum", "category": "museum", "rating": 3.0, "description": "Museum.",
          "lat": 9.49, "lng": 76.33}  # fmt: skip


async def test_a_plan_is_saved_with_its_weather_and_its_rainy_day_has_an_indoor_place(db_session):
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, _events, tools):
        tools["activities"].return_value = [*ATTRACTIONS, MUSEUM]
        await _login(client)
        body = {"destination": "Alleppey", "start_date": "2030-01-10", "end_date": "2030-01-12", "budget": 60_000,
                "group_size": 1, "interests": ["history"]}  # fmt: skip
        trip_id = (await client.post("/trips", json=body)).json()["id"]
        assert (await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "Backwaters"})).status_code == 202
        runs = await _run_finished(client, trip_id, orchestrator_rows=1)

        plan = (await client.get(f"/trips/{trip_id}/itinerary")).json()["structured_data"]
        assert [day["weather"]["kind"] for day in plan["days"]] == ["sunny", "rainy", "sunny"]
        indoor = {a["name"] for a in [*ATTRACTIONS, MUSEUM] if weather.setting(a["category"]) == "indoor"}
        assert {plan["days"][1][slot]["activity"] for slot in SLOTS if plan["days"][1].get(slot)} & indoor

        read = next(run for run in runs if run["agent_name"] == "weather")
        assert read["status"] == "completed" and read["output"]["rainy"] == ["2030-01-11"]
        timeline = {step["agent_name"]: step for step in (await client.get(f"/trips/{trip_id}/timeline")).json()}
        assert timeline["weather"]["detail"] == {"days": 3, "rainy": ["2030-01-11"], "estimated": 0}

        pdf = await client.get(f"/trips/{trip_id}/export/pdf")
        text = " ".join(" ".join(page.extract_text().split()) for page in PdfReader(io.BytesIO(pdf.content)).pages)
        assert "Rain · 30° / 22°" in text and "Sunny · 30° / 22°" in text


async def test_the_tool_gives_the_trips_days_the_forecast_then_the_climate_and_caches_them(tool_cache):  # noqa: F811
    providers = FakeProviders()
    today = date.today()
    async with tool_server(providers):
        soon = await call_tool(
            "get_weather", {"destination": "Goa", "date_range": f"{today} to {today + timedelta(days=6)}"}
        )
        again = await call_tool(
            "get_weather", {"destination": "Goa", "date_range": f"{today} to {today + timedelta(days=6)}"}
        )
        later = await call_tool(
            "get_weather",
            {"destination": "Goa", "date_range": f"{today + timedelta(days=30)} to {today + timedelta(days=32)}"},
        )
        unknown = await call_tool(
            "get_weather",
            {"destination": "Leh", "date_range": f"{today + timedelta(days=30)} to {today + timedelta(days=32)}"},
        )

    assert [day["date"] for day in soon] == [str(today + timedelta(days=n)) for n in range(7)]  # every day of it
    assert soon[0]["source"] == "forecast" and soon[0]["condition"] == "Clear"  # the stand-in OWM has today
    assert {day["source"] for day in soon[1:]} == {"climate"}  # and nothing after: the climate says the rest
    assert again == soon and providers.counts()["openweather"] == 1  # the second answer from Redis
    assert {day["source"] for day in later} == {"climate"} and len(later) == 3  # past the forecast: no request
    assert unknown == []  # a place with no climate data: no guess
    assert providers.counts()["openweather"] == 1
    assert len(families(tool_cache)["weather"]) == 3

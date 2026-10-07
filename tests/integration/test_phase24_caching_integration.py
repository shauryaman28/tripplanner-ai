"""Phase 24 — the tools' cache in a real Redis, and a trip that is created, warmed and planned.

    RUN_INTEGRATION=1 pytest tests/integration/test_phase24_caching_integration.py -v

Real Redis for the cache the MCP tools write (the TTLs are read back with
Redis's own TTL), real Postgres, the real route, the real background task, the
real MCP server and client (in this process), the real agents. The five
providers are fakes that count the requests they receive (tests/fakes.py).

The tools' cache is pointed at Redis database 15 for these tests: the keys are
built exactly as in production, and a developer's own cached searches in
database 0 are neither read nor overwritten with fake flights.
"""

import importlib.util
import logging
import os
import sys
import threading
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import redis

from src.ai.mcp_client.client import call_tool
from src.ai.mcp_server import cache
from src.ai.mcp_server.models import AttractionInput, FlightSearchInput, HotelSearchInput, ToolError, WeatherInput
from src.ai.mcp_server.tools import get_attractions, get_weather, search_flights, search_hotels
from src.ai.orchestrator.warming import start_cache_warming
from tests.fakes import FakeProviders, providers_faked, tool_server
from tests.integration.test_pipeline_integration import _login, _run_finished, _stack

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

START = date.today() + timedelta(days=30)
END = START + timedelta(days=3)
SOON = date.today().isoformat()
TOOL_CACHE_DB = 15

# scripts/ is not a package: load the report the way `python scripts/cache_ttls.py` runs it.
_spec = importlib.util.spec_from_file_location(
    "cache_ttls", Path(__file__).resolve().parents[2] / "scripts" / "cache_ttls.py"
)
cache_ttls = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cache_ttls  # a dataclass looks its own module up while it is being defined
_spec.loader.exec_module(cache_ttls)


@pytest.fixture
def tool_cache():
    """The tools' Redis cache on a database of its own; yields a client on it. Its mcp:* keys are removed around the test."""
    url = urlsplit(cache.mcp_settings.REDIS_URL)._replace(path=f"/{TOOL_CACHE_DB}").geturl()
    client = redis.from_url(url, decode_responses=True)

    def clear() -> None:
        for key in client.scan_iter("mcp:*"):
            client.delete(key)

    clear()
    with patch.object(cache.mcp_settings, "REDIS_URL", url):
        cache.reset_client()
        yield client
    clear()
    client.close()
    cache.reset_client()


def families(client) -> dict[str, list[str]]:
    """{family: its keys} of everything the tools have cached."""
    found: dict[str, list[str]] = {}
    for key in client.scan_iter("mcp:*"):
        found.setdefault(key.split(":")[1], []).append(key)
    return found


def test_every_cache_expires_when_the_spec_says_read_back_from_redis(tool_cache):
    """The roadmap's check — `TTL mcp:flights:*` → ~300, `TTL mcp:attractions:*` → ~21600 — for all four, and the fifth."""
    providers = FakeProviders()
    searches = [
        lambda: search_flights(
            FlightSearchInput(origin="DEL", destination="Goa", date=str(START), return_date=str(END), budget=50_000)
        ),
        lambda: search_hotels(
            HotelSearchInput(destination="Goa", check_in=str(START), check_out=str(END), budget_per_night=20_000)
        ),
        lambda: get_attractions(AttractionInput(destination="Goa", interests=["beach", "history"], limit=5)),
        lambda: get_weather(WeatherInput(destination="Goa", date_range=f"{SOON} to {SOON}")),
        lambda: get_weather(WeatherInput(destination="Goa", date_range=f"{START} to {END}")),
    ]

    with providers_faked(providers):
        first = [search() for search in searches]
        asked = providers.counts()
        second = [search() for search in searches]

    assert not any(isinstance(result, ToolError) for result in first), first
    assert second == first  # what comes back out of Redis is what went in
    assert providers.counts() == asked  # and none of it needed a provider

    keys = families(tool_cache)
    assert {family: len(found) for family, found in keys.items()} == {
        "flights": 1,
        "hotels": 1,
        "attractions": 1,
        "weather": 2,  # a forecast and a climate estimate
        "geocode": 1,
    }
    spec = {"flights": 300, "hotels": 900, "attractions": 21_600, "weather": 3_600, "geocode": 2_592_000}
    for family, seconds in spec.items():
        for key in keys[family]:
            assert seconds - 10 <= tool_cache.ttl(key) <= seconds, (key, tool_cache.ttl(key))

    # …which is what the script the run guide points at reports
    report = cache_ttls.report(tool_cache)
    assert [family.name for family in report] == ["flights", "hotels", "attractions", "weather", "geocode"]
    assert all(family.ok for family in report)
    assert cache_ttls.SPEC == {name: spec[name] for name in ("flights", "hotels", "attractions", "weather")}


def test_a_key_that_never_expires_or_outlives_the_spec_is_reported(tool_cache):
    tool_cache.set("mcp:flights:kept-for-ever", "[]")
    tool_cache.set("mcp:hotels:kept-too-long", "[]", ex=3_600)
    tool_cache.set("mcp:weather:fine", "[]", ex=3_600)
    tool_cache.set("mcp:restaurants:nobody-decided", "[]", ex=60)

    report = {family.name: family for family in cache_ttls.report(tool_cache)}

    assert report["flights"].soonest == -1 and not report["flights"].ok
    assert not report["hotels"].ok  # an hour, where the spec says fifteen minutes
    assert report["weather"].ok
    assert not report["restaurants"].ok  # a family with no TTL on record is not waved through
    assert "NEVER" in cache_ttls._line(report["flights"])


async def test_a_trip_is_created_at_once_warmed_behind_it_and_planned_from_the_cache(db_session, tool_cache, caplog):
    """POST /trips → the trip's searches go out in the background → POST /plan sends no request to any provider."""
    providers = FakeProviders()
    let_go = threading.Event()
    providers.on_request = lambda _provider: let_go.wait(10)  # no provider answers until the test says so
    started = []

    def start(trip: dict, user_id):
        started.append(start_cache_warming(trip, user_id))
        return started[-1]

    async with _stack(flight_tool=call_tool, hotel_tool=call_tool) as (client, _events, tools):
        tools["activities"].side_effect = call_tool  # the three agents and the warmer all reach the real tools
        tools["warm"].side_effect = call_tool
        async with tool_server(providers):
            await _login(client)
            saved = {"home_city": "Mumbai", "dietary_restrictions": ["vegetarian"], "preferred_airlines": ["AI"]}
            assert (await client.put("/users/preferences", json=saved)).status_code == 200

            # ── the trip is created while its searches are still out ──
            body = {
                "destination": "Goa",
                "start_date": str(START),
                "end_date": str(END),
                "budget": 60_000,
                "group_size": 2,
                "interests": ["history"],
            }
            with (
                patch("app.api.routes.trips.start_cache_warming", start),
                caplog.at_level(logging.INFO, logger="src.ai.orchestrator.warming"),
            ):
                created = await client.post("/trips", json=body)
                assert created.status_code == 201, created.text
                trip_id = created.json()["id"]
                (warming,) = started
                assert not warming.done()  # the answer did not wait for a single provider
                assert families(tool_cache) == {}

                let_go.set()
                await warming

            # ── warmed: with the traveller's saved preferences, read from the database ──
            assert f"[CACHE WARM] flights: BOM → Goa on {START}, back {END} — 2 result(s) ready in " in caplog.text
            assert "[CACHE WARM] attractions: Goa: history, vegetarian — " in caplog.text
            assert set(families(tool_cache)) == {"flights", "hotels", "attractions", "weather", "geocode"}
            asked_by_warming = providers.counts()
            assert asked_by_warming["duffel"] == 1 and asked_by_warming["liteapi"] == 1

            # ── planned: every search is a cache hit ──
            logged_before = len(caplog.records)
            with caplog.at_level(logging.INFO, logger="src.ai.mcp_server.cache"):
                resp = await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "A relaxed few days by the sea"})
                assert resp.status_code == 202, resp.text
                runs = await _run_finished(client, trip_id, orchestrator_rows=1)

            searches = {run["agent_name"]: run for run in runs if run["agent_name"].endswith("_agent")}
            assert {name: run["status"] for name, run in searches.items()} == {
                "flight_agent": "completed",
                "hotel_agent": "completed",
                "activities_agent": "completed",
            }
            assert providers.counts() == asked_by_warming  # the plan asked no provider anything
            lookups = [
                record.getMessage()
                for record in caplog.records[logged_before:]
                if record.name == "src.ai.mcp_server.cache"
            ]
            assert sorted(lookup.rsplit(":", 1)[0] for lookup in lookups) == [
                "[CACHE HIT]  mcp:attractions:v3",
                "[CACHE HIT]  mcp:flights",
                "[CACHE HIT]  mcp:hotels",
            ]  # three lookups, three hits, not one miss

            # the saved airline is applied to the cached flights: Air India first, though it is the dearer one
            flights = searches["flight_agent"]["output"]["flights"]
            assert [flight["airline"] for flight in flights] == ["AI", "6E"]
            assert (await client.get(f"/trips/{trip_id}")).json()["status"] == "completed"

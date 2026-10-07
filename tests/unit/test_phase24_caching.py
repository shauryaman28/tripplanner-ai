"""Phase 24 — what a search is cached under, one search per key at a time, and cache warming.

The tools run for real here, in front of fake providers (tests/fakes.py) and a
cache that is a dict: what is asserted is how many requests a provider
received. The TTLs are read back from a real Redis in
tests/integration/test_phase24_caching_integration.py.
"""

import asyncio
import logging
import threading
import time
import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.security import create_access_token
from app.models.user_preferences import UserPreferences
from src.ai.mcp_server import cache, tools
from src.ai.mcp_server.cache import single_flight
from src.ai.mcp_server.models import (
    AttractionInput,
    FlightSearchInput,
    HotelSearchInput,
    ToolError,
    WeatherInput,
)
from src.ai.mcp_server.tools import get_attractions, get_weather, search_flights, search_hotels
from src.ai.orchestrator import warming
from src.ai.orchestrator.orchestrator import (
    _search_activities,
    _search_flights,
    _search_hotels,
    apply_preferences_node,
)
from src.ai.orchestrator.warming import start_cache_warming, warm_trip_caches
from tests.fakes import FakeProviders, duffel_offer, memory_cache, providers_faked, tool_server

DAY = date.today() + timedelta(days=30)
START, END = DAY.isoformat(), (DAY + timedelta(days=4)).isoformat()  # four nights
SOON = date.today().isoformat()

FLIGHTS = {"origin": "DEL", "destination": "Goa", "date": START, "return_date": END, "budget": 50_000.0}
HOTELS = {"destination": "Goa", "check_in": START, "check_out": END, "budget_per_night": 20_000.0, "guests": 2}
TRIP = {
    "destination": "Goa",
    "start_date": START,
    "end_date": END,
    "budget": 60_000.0,
    "group_size": 2,
    "interests": ["beach", "history"],
}


def flights(**changes) -> list | ToolError:
    return search_flights(FlightSearchInput(**{**FLIGHTS, **changes}))


def hotels(**changes) -> list | ToolError:
    return search_hotels(HotelSearchInput(**{**HOTELS, **changes}))


# ── What a search is cached under ──────────────────────────────────────────


def test_the_same_flight_search_under_another_budget_is_a_cache_hit():
    """Duffel is never told the budget or the airlines: neither may decide whether it is asked again."""
    providers = FakeProviders()  # two offers: 6E at ₹4,200 and AI at ₹6,100

    with providers_faked(providers), memory_cache() as kept:
        everything = flights(budget=50_000)
        cheaper = flights(budget=5_000)
        nothing = flights(budget=3_000)
        air_india_first = flights(budget=50_000, preferred_airlines=["AI"])

    assert [f.price_inr for f in everything] == [4200.0, 6100.0]
    assert [f.price_inr for f in cheaper] == [4200.0]
    assert (nothing.code, nothing.error) == ("NO_RESULTS", "No flights from DEL to GOI within ₹3,000.")
    assert [f.airline for f in air_india_first] == ["AI", "6E"]
    assert providers.counts() == {"duffel": 1}
    assert len(kept) == 1


def test_a_city_and_its_airport_code_are_one_search():
    providers = FakeProviders()

    with providers_faked(providers), memory_cache():
        flights(destination="Goa")
        flights(destination="GOI")
        flights(destination=" goa ", origin="Delhi")

    assert providers.counts() == {"duffel": 1}


@pytest.mark.parametrize(
    "change",
    [
        {"date": (DAY + timedelta(days=1)).isoformat()},
        {"return_date": None},
        {"passengers": 2},
        {"max_stops": 2},  # a re-plan allows one more stop: it must not be served the first search's answer
        {"origin": "BOM"},
        {"destination": "Jaipur"},
    ],
)
def test_whatever_the_flight_provider_is_asked_differently_is_another_search(change):
    providers = FakeProviders()

    with providers_faked(providers), memory_cache() as kept:
        flights()
        flights(**change)

    assert providers.counts() == {"duffel": 2}
    assert len(kept) == 2


@pytest.mark.parametrize("budget", [2_500, 4_000, 5_500, 7_000, 8_500, 10_000, 20_000])
def test_the_five_cheapest_flights_are_all_that_any_budget_needs(budget):
    """Eight offers come back and five are kept. Under any budget, the answer is the one the full list would give."""
    prices = [9000, 3000, 7000, 5000, 4000, 8000, 6000, 3500]
    providers = FakeProviders()
    providers.offers = [duffel_offer(number=str(n), amount=f"{price}.00") for n, price in enumerate(prices)]

    with providers_faked(providers), memory_cache() as kept:
        flights(budget=50_000)  # fills the cache
        answer = flights(budget=budget)

    ((stored, _ttl),) = kept.values()
    assert [f["price_inr"] for f in stored] == [3000, 3500, 4000, 5000, 6000]
    expected = sorted(price for price in prices if price <= budget)[:5]
    found = [] if isinstance(answer, ToolError) else [f.price_inr for f in answer]
    assert found == expected
    assert providers.counts() == {"duffel": 1}


def test_a_flight_search_nobody_could_afford_is_still_an_answer_worth_keeping():
    providers = FakeProviders()

    with providers_faked(providers), memory_cache() as kept:
        too_dear = flights(budget=3_000)
        later = flights(budget=50_000)

    assert too_dear.code == "NO_RESULTS"
    assert len(kept) == 1
    assert len(later) == 2
    assert providers.counts() == {"duffel": 1}


def test_an_empty_answer_is_not_kept():
    providers = FakeProviders()
    providers.offers = []

    with providers_faked(providers), memory_cache() as kept:
        first = flights()
        second = flights()

    assert first.code == second.code == "NO_RESULTS"
    assert kept == {}
    assert providers.counts() == {"duffel": 2}  # the next search asks again


def test_the_same_stay_under_another_nightly_budget_is_a_cache_hit():
    """The nightly budget is what is left after the flights — different on every re-plan of the same stay."""
    providers = FakeProviders()  # ₹18,000 and ₹26,000 for four nights: ₹4,500 and ₹6,500 a night

    with providers_faked(providers), memory_cache() as kept:
        both = hotels(budget_per_night=20_000)
        one = hotels(budget_per_night=5_000)
        none = hotels(budget_per_night=1_000)

    assert [h.price_per_night_inr for h in both] == [4500.0, 6500.0]
    assert [h.name for h in one] == ["Goa Grand"]
    assert none.code == "NO_RESULTS"
    assert none.error == "No hotels within ₹1,000/night in Goa. The cheapest available is ₹4,500/night."
    assert providers.counts() == {"liteapi": 1}
    assert len(kept) == 1


@pytest.mark.parametrize(
    "change", [{"guests": 3}, {"check_out": (DAY + timedelta(days=5)).isoformat()}, {"destination": "Jaipur"}]
)
def test_whatever_the_hotel_provider_is_asked_differently_is_another_search(change):
    providers = FakeProviders()

    with providers_faked(providers), memory_cache():
        hotels()
        hotels(**change)

    assert providers.counts() == {"liteapi": 2}


def test_every_cache_is_kept_for_as_long_as_the_spec_says():
    """Phase 3's four TTLs, and the geocoder's. Read back from Redis itself in the integration test."""
    providers = FakeProviders()

    with providers_faked(providers), memory_cache() as kept:
        flights()
        hotels()
        get_attractions(AttractionInput(destination="Goa", interests=["beach"], limit=5))
        get_weather(WeatherInput(destination="Goa", date_range=f"{SOON} to {SOON}"))  # a forecast
        get_weather(WeatherInput(destination="Goa", date_range=f"{START} to {END}"))  # a climate estimate

    assert all(key.startswith("mcp:") and len(key.rsplit(":", 1)[1]) == 32 for key in kept)
    ttls = {key.split(":")[1]: set() for key in kept}
    for key, (_value, ttl) in kept.items():
        ttls[key.split(":")[1]].add(ttl)
    assert ttls == {
        "flights": {300},  # 5 minutes
        "hotels": {900},  # 15 minutes
        "attractions": {21_600},  # 6 hours
        "weather": {3_600},  # 1 hour — both kinds
        "geocode": {2_592_000},  # 30 days: not in the spec; places do not move
    }


# ── One search per key at a time ───────────────────────────────────────────


def two_at_once(search, provider: str, providers: FakeProviders) -> list:
    """Run `search` twice, the second starting while the first is still at its provider."""
    at_provider, let_go = threading.Event(), threading.Event()

    def hold(name: str) -> None:
        if name == provider:
            at_provider.set()
            let_go.wait(5)

    providers.on_request = hold
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(search())) for _ in range(2)]
    threads[0].start()
    assert at_provider.wait(5)
    threads[1].start()
    time.sleep(0.15)  # long enough for the second to reach the provider too, if nothing held it back
    let_go.set()
    for thread in threads:
        thread.join(5)
    return results


@pytest.mark.parametrize(
    "search, provider, size",
    [
        (lambda: flights(), "duffel", 2),
        (lambda: hotels(), "liteapi", 2),
        (lambda: tools._geocode("Goa"), "nominatim", 2),  # (lat, lon)
    ],
)
def test_two_identical_searches_at_once_ask_the_provider_once(search, provider, size):
    """Without the hold, both miss the cache and both go out — a trip being warmed and planned at once does that."""
    providers = FakeProviders()

    with providers_faked(providers), memory_cache():
        first, second = two_at_once(search, provider, providers)

    assert providers.counts() == {provider: 1}
    assert first == second and len(first) == size  # and the one that waited got the same answer


def test_different_searches_do_not_wait_for_each_other():
    reached = threading.Event()

    def other() -> None:
        with single_flight("mcp:flights:another-search"):
            reached.set()

    with single_flight("mcp:flights:one-search"):
        thread = threading.Thread(target=other)
        thread.start()
        assert reached.wait(2)
        thread.join(2)

    assert cache._in_flight == {}  # nothing is remembered once nobody holds or waits


def test_a_search_that_fails_lets_the_next_one_through():
    with pytest.raises(RuntimeError):
        with single_flight("mcp:hotels:a-search"):
            raise RuntimeError("the provider was down")

    got_in = threading.Event()

    def next_search() -> None:
        with single_flight("mcp:hotels:a-search"):
            got_in.set()

    thread = threading.Thread(target=next_search, daemon=True)  # in a thread: a key still held would hang the test
    thread.start()
    assert got_in.wait(2)
    thread.join(2)
    assert cache._in_flight == {}


def test_searches_that_start_together_share_one_redis_connection():
    """Three tools reach for the cache in the same instant, each in its own thread: one of them connects."""
    connected = []

    def connect(*_args, **_kwargs):
        time.sleep(0.05)  # long enough for every thread to be here at once
        connected.append(MagicMock())
        return connected[-1]

    start = threading.Barrier(5)
    clients = []

    def reach() -> None:
        start.wait(5)
        clients.append(cache._get_client())

    cache.reset_client()
    try:
        with patch("redis.from_url", connect):
            threads = [threading.Thread(target=reach) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
    finally:
        cache.reset_client()

    assert len(connected) == 1
    assert clients == [connected[0]] * 5


# ── Cache warming: the searches it makes ───────────────────────────────────


def warmed_by(tool_mock: AsyncMock) -> dict[str, dict]:
    """{tool name: the params it was called with}."""
    return {call.args[0]: call.args[1] for call in tool_mock.await_args_list}


async def test_warming_makes_the_searches_a_plan_opens_with():
    tool = AsyncMock(return_value=[{}, {}])

    with patch.object(warming, "call_tool", tool):
        outcome = await warm_trip_caches(TRIP)

    assert outcome == {"flights": "warmed", "hotels": "warmed", "weather": "warmed", "attractions": "warmed"}
    assert warmed_by(tool) == {
        "search_flights": {
            "origin": "DEL",  # nothing says otherwise: what planning assumes too
            "destination": "Goa",
            "date": START,
            "return_date": END,
            "budget": 60_000.0,
            "passengers": 2,
            "max_stops": 1,
        },
        "search_hotels": {
            "destination": "Goa",
            "check_in": START,
            "check_out": END,
            "budget_per_night": 15_000.0,  # the whole budget over four nights: not part of the cache key
            "guests": 2,
        },
        "get_weather": {"destination": "Goa", "date_range": f"{START} to {END}"},
        "get_attractions": {"destination": "Goa", "interests": ["beach", "history"], "limit": 10},
    }


async def test_warming_says_what_it_did_in_the_log(caplog):
    """The roadmap's acceptance line: `[CACHE WARM] flights:...` visible in the logs."""

    async def tool(name: str, _params: dict):
        return (
            ToolError(error="No hotels within ₹15,000/night in Goa.", code="NO_RESULTS")
            if "hotels" in name
            else [{}] * 3
        )

    with patch.object(warming, "call_tool", tool), caplog.at_level(logging.INFO, logger="src.ai.orchestrator.warming"):
        outcome = await warm_trip_caches(TRIP)

    assert f"[CACHE WARM] flights: DEL → Goa on {START}, back {END} — 3 result(s) ready in " in caplog.text
    assert "[CACHE WARM] attractions: Goa: beach, history — 3 result(s) ready in " in caplog.text
    assert f"[CACHE WARM] weather: Goa, {START} to {END} — 3 result(s) ready in " in caplog.text
    assert f"[CACHE WARM] hotels: Goa, {START} to {END}, 2 guest(s) — answered NO_RESULTS in " in caplog.text
    assert outcome["hotels"] == "NO_RESULTS" and outcome["flights"] == "warmed"  # one failure stops nothing else


async def test_saved_preferences_shape_the_warmed_searches():
    tool = AsyncMock(return_value=[{}])
    saved = {
        "home_city": "Mumbai",
        "dietary_restrictions": ["vegetarian"],
        "preferred_airlines": ["AI"],
        "travel_style": None,
    }

    with patch.object(warming, "call_tool", tool):
        await warm_trip_caches(TRIP, saved)

    searches = warmed_by(tool)
    assert searches["search_flights"]["origin"] == "BOM"
    assert searches["search_flights"]["preferred_airlines"] == ["AI"]
    assert searches["get_attractions"]["interests"] == ["beach", "history", "vegetarian"]


async def test_a_trip_that_names_no_interests_warms_no_attractions():
    """Planning will ask the traveller for them, or read them from the request: that search is not known yet."""
    tool = AsyncMock(return_value=[{}])

    with patch.object(warming, "call_tool", tool):
        outcome = await warm_trip_caches({**TRIP, "interests": []}, {"dietary_restrictions": ["vegan"]})

    assert set(outcome) == {"flights", "hotels", "weather"}
    assert "get_attractions" not in warmed_by(tool)


@pytest.mark.parametrize("destination", ["A relaxed week somewhere in Kerala", "", "   "])
async def test_a_destination_that_is_not_yet_a_place_warms_nothing(destination, caplog):
    tool = AsyncMock(return_value=[{}])

    with patch.object(warming, "call_tool", tool), caplog.at_level(logging.INFO, logger="src.ai.orchestrator.warming"):
        outcome = await warm_trip_caches({**TRIP, "destination": destination})

    assert outcome == {}
    tool.assert_not_awaited()
    assert "[CACHE WARM] skipped" in caplog.text


async def test_warming_never_raises_whatever_the_search_does(caplog):
    tool = AsyncMock(side_effect=RuntimeError("the MCP server is gone"))

    with patch.object(warming, "call_tool", tool):
        outcome = await warm_trip_caches(TRIP)

    assert set(outcome.values()) == {"MCP_CLIENT_ERROR"}
    assert "[CACHE WARM] flights: " in caplog.text and "the search failed: the MCP server is gone" in caplog.text


# ── Cache warming: planning finds it ───────────────────────────────────────


async def plan_state(trip: dict, saved: UserPreferences | None = None) -> dict:
    """The state a plan of `trip` searches with — through the graph's own preferences node."""
    state = {**trip, "db": object(), "trip_id": uuid.uuid4()}
    with (
        patch("src.ai.orchestrator.orchestrator.get_trip_user_id", AsyncMock(return_value=uuid.uuid4())),
        patch("src.ai.orchestrator.orchestrator.load_preferences", AsyncMock(return_value=saved)),
        patch("src.ai.orchestrator.orchestrator._log", AsyncMock()),
    ):
        state = await apply_preferences_node(state)
    return {key: value for key, value in state.items() if key not in ("db", "trip_id")}


async def plan_searches(state: dict) -> dict:
    """A plan's three searches, in the graph's order: flights, then hotels on what the flights left, and activities."""
    found = await _search_flights(state)
    state = {**state, **found, "budget_decision": {"remaining_budget": state["budget"] - 4_200.0}}
    found.update(await _search_hotels(state))
    found.update(await _search_activities(state))
    return found


SAVED = UserPreferences(
    user_id=uuid.uuid4(),
    home_city="Mumbai",
    dietary_restrictions=["vegetarian"],
    preferred_airlines=["AI"],
    travel_style="mid-range",
)
SAVED_AS_LOADED = {
    "home_city": "Mumbai",
    "dietary_restrictions": ["vegetarian"],
    "preferred_airlines": ["AI"],
    "travel_style": "mid-range",
}


@pytest.mark.parametrize(
    "saved, as_loaded", [(None, None), (SAVED, SAVED_AS_LOADED)], ids=["no preferences", "preferences"]
)
async def test_a_plan_finds_everything_warming_cached(saved, as_loaded):
    """The point of the phase: after warming, a plan's searches send no request to any provider.

    Real tools, real cache keys, real agents and orchestrator search functions.
    If warming and planning ever build a search differently, this is where it shows.
    """
    providers = FakeProviders()

    with memory_cache() as kept:
        async with tool_server(providers):
            outcome = await warm_trip_caches(TRIP, as_loaded)
            asked_by_warming = providers.counts()

            found = await plan_searches(await plan_state(TRIP, saved))

    assert set(outcome.values()) == {"warmed"}
    assert asked_by_warming["duffel"] == 1 and asked_by_warming["liteapi"] == 1 and asked_by_warming["opentripmap"] >= 2
    assert providers.counts() == asked_by_warming  # the plan asked no provider anything
    assert (found["flight_error"], found["hotel_error"], found["activities_error"]) == (None, None, None)
    assert len(found["flights"]) == 2 and len(found["hotels"]) == 2 and found["attractions"]
    assert {key.split(":")[1] for key in kept} == {"flights", "hotels", "attractions", "weather", "geocode"}
    if saved:
        assert found["flights"][0]["airline"] == "AI"  # the preference is applied to the cached answer


async def test_a_plan_from_another_city_misses_and_asks_for_itself():
    providers = FakeProviders()

    with memory_cache():
        async with tool_server(providers):
            await warm_trip_caches(TRIP)
            state = await plan_state(TRIP)
            found = await _search_flights({**state, "origin": "BLR"})  # "…flying from Bengaluru"

    assert found["flight_error"] is None
    assert providers.counts()["duffel"] == 2


async def test_a_plan_that_starts_while_warming_is_under_way_waits_for_its_answer():
    """Warming's flight search is still at the provider when planning asks for the same flights: one request, not two."""
    providers = FakeProviders()
    at_provider, let_go = threading.Event(), threading.Event()

    def hold(provider: str) -> None:
        if provider == "duffel":
            at_provider.set()
            let_go.wait(5)

    providers.on_request = hold
    with memory_cache():
        async with tool_server(providers):
            warming_task = asyncio.create_task(warm_trip_caches(TRIP))
            assert await asyncio.to_thread(at_provider.wait, 5)

            planning = asyncio.create_task(_search_flights(await plan_state(TRIP)))
            await asyncio.sleep(0.2)
            assert not planning.done()  # it is waiting…
            assert providers.counts()["duffel"] == 1  # …and has sent nothing of its own

            let_go.set()
            await warming_task
            found = await planning

    assert found["flight_error"] is None and len(found["flights"]) == 2
    assert providers.counts()["duffel"] == 1


# ── Cache warming: when it starts ──────────────────────────────────────────


async def create_trip(body: dict) -> tuple[int, uuid.UUID]:
    """POST /trips as a signed-in traveller, on a database that is a mock. Returns (status, the traveller's id)."""
    from app.db.session import get_db
    from app.main import app

    user_id = uuid.uuid4()
    session = AsyncMock()
    session.get = AsyncMock(return_value=MagicMock(id=user_id))
    session.add = MagicMock()

    async def database():
        yield session

    app.dependency_overrides[get_db] = database
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = create_access_token(str(user_id))
            response = await client.post("/trips", json=body, headers={"Authorization": f"Bearer {token}"})
    finally:
        app.dependency_overrides.pop(get_db, None)
    return response.status_code, user_id


async def test_creating_a_trip_starts_warming_its_caches(no_cache_warming):
    status, user_id = await create_trip({**TRIP, "budget": 60_000})

    assert status == 201
    no_cache_warming.assert_called_once_with(TRIP, user_id)  # the very state planning will start from


async def test_warming_can_be_switched_off(no_cache_warming):
    with patch.object(settings, "CACHE_WARMING_ENABLED", False):
        status, _ = await create_trip({**TRIP, "budget": 60_000})

    assert status == 201
    no_cache_warming.assert_not_called()


async def test_a_trip_that_is_refused_warms_nothing(no_cache_warming):
    status, _ = await create_trip({**TRIP, "end_date": START, "start_date": END})

    assert status == 422
    no_cache_warming.assert_not_called()


async def test_warming_runs_in_the_background_with_the_travellers_saved_preferences():
    user_id = uuid.uuid4()
    started = asyncio.Event()

    async def searches(trip: dict, preferences: dict | None) -> dict:
        await started.wait()  # still running after start_cache_warming has returned
        return {}

    with (
        patch.object(warming, "_saved_preferences", AsyncMock(return_value=SAVED_AS_LOADED)) as load,
        patch.object(warming, "warm_trip_caches", AsyncMock(side_effect=searches)) as warm,
    ):
        task = start_cache_warming(TRIP, user_id)
        assert isinstance(task, asyncio.Task) and not task.done()
        started.set()
        await task

    load.assert_awaited_once_with(user_id)
    warm.assert_awaited_once_with(TRIP, SAVED_AS_LOADED)


async def test_the_background_task_ends_in_a_log_line_never_an_error(caplog):
    with patch.object(warming, "_saved_preferences", AsyncMock(side_effect=ConnectionError("the database is down"))):
        task = start_cache_warming(TRIP, uuid.uuid4())
        await task

    assert task.exception() is None
    assert "[CACHE WARM] failed — planning will make its own searches" in caplog.text


# ── The log lines are there to be seen ─────────────────────────────────────


def test_the_apps_info_lines_are_shown_when_nothing_else_shows_them():
    """Uvicorn sets up only its own loggers: without this, "[CACHE WARM] flights: …" is written and never seen."""
    from app.main import _show_app_logs, app

    root, app_log, planner_log = logging.getLogger(), logging.getLogger("app"), logging.getLogger("src")
    before = (root.handlers[:], app_log.handlers[:], planner_log.handlers[:], app_log.level, planner_log.level)
    root.handlers, app_log.handlers, planner_log.handlers = [], [], []
    try:
        _show_app_logs()
        _show_app_logs()  # a reload must not show every line twice

        assert len(app_log.handlers) == 1 and planner_log.handlers == app_log.handlers
        assert logging.getLogger("src.ai.orchestrator.warming").isEnabledFor(logging.INFO)
        assert logging.getLogger("app.main").isEnabledFor(logging.INFO)

        # …and a process that already has logging set up is left alone
        app_log.handlers, planner_log.handlers = [], []
        root.handlers = [logging.NullHandler()]
        _show_app_logs()
        assert app_log.handlers == [] and planner_log.handlers == []
    finally:
        root.handlers, app_log.handlers, planner_log.handlers = before[0], before[1], before[2]
        app_log.setLevel(before[3])
        planner_log.setLevel(before[4])

    assert app.version >= "0.24.0"  # the phase this arrived in; each phase's own tests say which it is now

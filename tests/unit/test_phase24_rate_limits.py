"""Phase 24 — rate limits, backoff, and tools that run side by side.

Three layers, each tested where it lives:

- RateLimiter (mcp_server/rate_limiter.py) on a clock the test owns, then on
  real threads and real time;
- outbound.send — which answers are tried again, how long the waits are, what
  the log says;
- the real MCP server in front of fake providers (tests/fakes.tool_server): a
  forced 429 from the flight provider, three agents searching at once, two
  searches with their providers at the same moment.

No test sits through a wait: the autouse `no_waiting` fixture (tests/conftest.py)
records them instead. The ones that need real time say so.
"""

import asyncio
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from src.ai.agents.flight_agent import FlightAgent
from src.ai.mcp_client.client import call_tool
from src.ai.mcp_server import outbound, rate_limiter
from src.ai.mcp_server.models import AttractionInput, FlightSearchInput, HotelSearchInput, ToolError, WeatherInput
from src.ai.mcp_server.outbound import ATTEMPTS, send
from src.ai.mcp_server.rate_limiter import LIMITS, Limit, QueueFull, RateLimiter, StillLimited
from src.ai.mcp_server.server import TOOLS, mcp
from src.ai.mcp_server.tools import get_attractions, get_weather, search_flights, search_hotels
from src.ai.utils.failures import can_retry, failure_words
from tests.fakes import FakeProviders, memory_cache, provider_answer, providers_faked, tool_server

DAY = date.today() + timedelta(days=30)
START, END = DAY.isoformat(), (DAY + timedelta(days=4)).isoformat()
SOON = date.today().isoformat()  # inside the weather provider's five-day forecast

FLIGHTS = {"origin": "DEL", "destination": "Goa", "date": START, "return_date": END, "budget": 50_000.0}
HOTELS = {"destination": "Goa", "check_in": START, "check_out": END, "budget_per_night": 20_000.0, "guests": 2}
BEACHES = AttractionInput(destination="Goa", interests=["beach"], limit=5)
WEATHER_SOON = WeatherInput(destination="Goa", date_range=f"{SOON} to {SOON}")


class Clock:
    """Time the test owns: sleeping moves it on."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def limiter_on(clock: Clock, calls: int, period: float, **options) -> RateLimiter:
    return RateLimiter("test", calls, period, clock=clock, sleep=clock.sleep, **options)


# ── RateLimiter ────────────────────────────────────────────────────────────


def test_requests_within_the_limit_do_not_wait():
    clock = Clock()
    limiter = limiter_on(clock, calls=3, period=10)

    assert [limiter.acquire() for _ in range(3)] == [0, 0, 0]
    assert clock.now == 0


def test_the_request_over_the_limit_waits_until_the_window_has_room():
    clock = Clock()
    limiter = limiter_on(clock, calls=2, period=10)
    limiter.acquire()  # at 0
    clock.now = 1
    limiter.acquire()  # at 1

    assert limiter.acquire() == 9  # the first request leaves the window at 10
    assert clock.now == 10
    assert limiter.acquire() == 1  # and the second at 11
    assert clock.now == 11


def test_a_limit_that_has_gone_quiet_is_free_again():
    clock = Clock()
    limiter = limiter_on(clock, calls=2, period=10)
    limiter.acquire()
    limiter.acquire()

    clock.now = 60
    assert limiter.acquire() == 0


def test_no_stretch_of_one_period_ever_holds_more_than_the_limit():
    """Whatever the arrivals — bursts, trickles, pauses — request i goes a full period after request i - calls."""
    clock = Clock()
    calls, period = 4, 10.0
    limiter = limiter_on(clock, calls=calls, period=period)
    gaps = [0, 0, 0, 0, 0, 0.5, 3, 0, 0, 12, 0, 0, 0, 0, 0, 0, 7, 0.1, 0.1, 25, 0, 0, 0, 0, 0, 0, 0, 0]
    sent = []
    for gap in gaps:
        clock.now += gap
        limiter.acquire()
        sent.append(clock.now)

    assert all(later - earlier >= period for earlier, later in zip(sent, sent[calls:]))
    assert any(later - earlier == period for earlier, later in zip(sent, sent[calls:]))  # and no longer than needed


def test_waiting_requests_are_given_their_turns_in_order():
    """Three callers arrive together at a one-at-a-time limit: they are spaced a period apart, not all sent at once."""
    asked: list[float] = []
    limiter = RateLimiter("test", 1, 10, clock=lambda: 0.0, sleep=asked.append)  # a clock that stands still

    assert [limiter.acquire() for _ in range(3)] == [0, 10, 20]
    assert asked == [10, 20]


def test_a_queue_too_long_to_join_is_refused_and_takes_no_turn():
    clock = Clock()
    limiter = RateLimiter("duffel", 1, 10, max_wait=15, clock=clock, sleep=lambda _seconds: None)
    limiter.acquire()  # goes at 0
    limiter.acquire()  # given 10

    with pytest.raises(QueueFull) as refused:  # would be given 20: too far off
        limiter.acquire()
    assert refused.value.provider == "duffel"
    assert refused.value.wait == 20
    assert "duffel" in str(refused.value)

    clock.now = 5
    assert limiter.acquire() == 15  # the turn at 20 is still there for whoever comes in time


def test_threads_sharing_a_limiter_keep_the_limit_in_real_time():
    """Fifteen threads at once, five allowed per 0.2 s: on a real clock they leave in three groups of five.

    A thread notes the time a few milliseconds after it is let go — fifteen of
    them share one interpreter — so the groups are told apart with a margin a
    quarter of the period wide.
    """
    calls, period, margin = 5, 0.2, 0.05
    limiter = RateLimiter("test", calls, period)
    sent: list[float] = []
    start = threading.Barrier(15)

    def request() -> None:
        start.wait(timeout=5)
        limiter.acquire()
        sent.append(time.monotonic())  # list.append is atomic

    threads = [threading.Thread(target=request) for _ in range(15)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    sent.sort()
    assert len(sent) == 15
    assert all(later - earlier >= period - margin for earlier, later in zip(sent, sent[calls:]))
    assert sent[-1] - sent[0] >= 2 * period - margin  # fifteen requests need three windows


def test_one_caller_at_a_time_decides_its_turn():
    """While one caller is working out its turn, the next waits outside — or two could be given the same one.

    The first caller is held at the clock, which it reads as its first step
    inside. A limiter without its lock lets the second caller read the clock too.
    """
    inside, let_go = threading.Event(), threading.Event()
    readings: list[int] = []

    def clock() -> float:
        readings.append(threading.get_ident())
        if len(readings) == 1:
            inside.set()
            let_go.wait(5)
        return 0.0

    limiter = RateLimiter("test", 1, 10, clock=clock, sleep=lambda _seconds: None)
    waits: list[float] = []
    threads = [threading.Thread(target=lambda: waits.append(limiter.acquire())) for _ in range(2)]
    threads[0].start()
    assert inside.wait(5)
    threads[1].start()
    time.sleep(0.1)  # long enough for the second caller to get in, if nothing kept it out

    assert len(readings) == 1
    let_go.set()
    for thread in threads:
        thread.join(5)
    assert sorted(waits) == [0, 10]  # and it was given the next turn, not the same one


def test_the_limits_on_record_are_the_providers_own():
    """Measured or documented (docs/phase24_build_log.md). A change here is a change of fact, not of taste."""
    assert LIMITS == {
        "duffel": Limit(30, 60),
        "liteapi": Limit(5, 1),
        "opentripmap": Limit(10, 1),
        "openweather": Limit(60, 60),
        "nominatim": Limit(1, 1),
    }


def test_a_provider_has_one_limiter_with_a_window_a_little_longer_than_its_own():
    duffel = rate_limiter.limiter("duffel")

    assert rate_limiter.limiter("duffel") is duffel
    assert (duffel.calls, duffel.period) == (30, 66.0)
    assert rate_limiter.limiter("nominatim").period == pytest.approx(1.1)
    with pytest.raises(KeyError):
        rate_limiter.limiter("a provider nobody measured")


# ── outbound.send: what is tried again, and after how long ─────────────────


def answers(*replies):
    """A request function that gives these replies in turn (a status, or an exception to raise)."""
    request = MagicMock(
        side_effect=[reply if isinstance(reply, Exception) else provider_answer(reply) for reply in replies]
    )
    return request


def backoffs(waits: list[tuple[str, float]]) -> list[float]:
    return [seconds for kind, seconds in waits if kind == "backoff"]


def logged_waits(caplog) -> list[tuple[float, int]]:
    """(seconds, the attempt waited for) of every [BACKOFF] line."""
    found = re.findall(r"\[BACKOFF\] \w+: .+ — waiting ([\d.]+) s before attempt (\d) of 4", caplog.text)
    return [(float(seconds), int(attempt)) for seconds, attempt in found]


def test_a_429_is_tried_again_after_waits_that_roughly_double(no_waiting, caplog):
    """The roadmap's check: backoff fires on a forced 429, with roughly doubling waits + jitter — read from the log."""
    request = answers(429, 429, 429, 200)

    with caplog.at_level(logging.WARNING, logger="src.ai.mcp_server.outbound"):
        response = send("duffel", request)

    assert response.status_code == 200
    assert request.call_count == 4
    first, second, third = backoffs(no_waiting)
    assert 1 <= first <= 2  # 1 s, plus up to a second of jitter
    assert 2 <= second <= 3  # 2 s
    assert 4 <= third <= 5  # 4 s
    # …and the log says so, wait by wait
    assert logged_waits(caplog) == [(round(first, 2), 2), (round(second, 2), 3), (round(third, 2), 4)]
    assert "[BACKOFF] duffel: HTTP 429 — waiting" in caplog.text


def test_the_waits_are_jittered_not_a_fixed_ladder(no_waiting):
    for _ in range(6):
        send("duffel", answers(429, 200))

    first_waits = backoffs(no_waiting)
    assert len(first_waits) == 6
    assert len(set(first_waits)) > 1  # six requests turned away together do not come back together


def test_a_provider_that_says_429_four_times_is_given_up_on(no_waiting):
    request = answers(429, 429, 429, 429, 200)

    with pytest.raises(StillLimited) as given_up:
        send("duffel", request)

    assert request.call_count == ATTEMPTS == 4  # the fifth answer was never asked for
    assert len(backoffs(no_waiting)) == 3
    assert str(given_up.value) == "duffel: still HTTP 429 after 4 attempts"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_server_error_is_tried_again(status, no_waiting):
    request = answers(status, 200)

    assert send("liteapi", request).status_code == 200
    assert request.call_count == 2
    assert len(backoffs(no_waiting)) == 1


def test_a_server_error_that_stays_is_raised_as_itself(no_waiting):
    request = answers(503, 503, 503, 503)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        send("liteapi", request)

    assert raised.value.response.status_code == 503
    assert request.call_count == 4


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_an_answer_that_a_second_try_cannot_change_is_not_tried_again(status, no_waiting):
    request = answers(status, 200)

    with pytest.raises(httpx.HTTPStatusError):
        send("duffel", request)

    assert request.call_count == 1
    assert no_waiting == []


def test_a_connection_never_made_is_tried_again_and_a_read_timeout_is_not(no_waiting):
    unreachable = answers(httpx.ConnectError("no route"), httpx.ConnectTimeout("no answer"), 200)
    assert send("duffel", unreachable).status_code == 200
    assert unreachable.call_count == 3

    slow = answers(httpx.ReadTimeout("the provider had the request and its full time"), 200)
    with pytest.raises(httpx.ReadTimeout):
        send("duffel", slow)
    assert slow.call_count == 1


def turned_away(headers: dict) -> httpx.Response:
    return provider_answer(429, headers=headers)


@pytest.mark.parametrize(
    "headers, low, high",
    [
        ({"Retry-After": "7"}, 7, 7),  # seconds
        ({"Retry-After": "120"}, 30, 30),  # never longer than the cap
        ({"Retry-After": "0"}, 1, 2),  # never shorter than the backoff
        ({"Retry-After": "soon"}, 1, 2),  # nonsense: the backoff
        ({}, 1, 2),
    ],
)
def test_a_provider_that_says_how_long_to_stay_away_is_listened_to(headers, low, high, no_waiting):
    request = MagicMock(side_effect=[turned_away(headers), provider_answer(200)])

    send("duffel", request)

    (wait,) = backoffs(no_waiting)
    assert low <= wait <= high


def test_a_reset_time_is_read_as_the_wait_until_then(no_waiting):
    """Duffel names the moment its count resets (`ratelimit-reset`, an HTTP date), not a number of seconds."""
    reset = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=12), usegmt=True)
    request = MagicMock(side_effect=[turned_away({"ratelimit-reset": reset}), provider_answer(200)])

    send("duffel", request)

    (wait,) = backoffs(no_waiting)
    assert 10 <= wait <= 12  # the header has whole seconds


def test_every_attempt_takes_its_turn_in_the_rate_limit():
    turns = MagicMock()

    with patch.object(outbound, "limiter", return_value=turns) as limiter_for:
        send("opentripmap", answers(503, 429, 200))

    limiter_for.assert_called_with("opentripmap")
    assert turns.acquire.call_count == 3  # a retry is a request like any other


def test_a_wait_in_the_log_never_carries_the_request(caplog):
    """An httpx error's text holds the URL, and two providers take their key in it."""
    error = httpx.ConnectError("failed: https://api.opentripmap.com/0.1/en/places/radius?apikey=SECRET-KEY")

    with caplog.at_level(logging.WARNING, logger="src.ai.mcp_server.outbound"):
        send("opentripmap", answers(error, 200))

    assert "[BACKOFF] opentripmap: ConnectError — waiting" in caplog.text
    assert "SECRET-KEY" not in caplog.text


# ── The tools: a 429 that outlasts the retries has a name ──────────────────


def test_a_forced_429_from_the_flight_provider_is_caught_and_the_search_still_answers(no_waiting):
    providers = FakeProviders()
    providers.turn_away["duffel"] = [429, 429]

    with providers_faked(providers), memory_cache() as cache:
        flights = search_flights(FlightSearchInput(**FLIGHTS))

    assert [flight.price_inr for flight in flights] == [4200.0, 6100.0]
    assert providers.answers == [("duffel", 429), ("duffel", 429), ("duffel", 200)]
    first, second = backoffs(no_waiting)
    assert 1 <= first <= 2 <= second <= 3
    assert len(cache) == 1  # stored once, after the answer that counted


def test_a_flight_provider_that_keeps_saying_429_is_a_rate_limited_search(no_waiting):
    providers = FakeProviders()
    providers.turn_away["duffel"] = [429] * 10

    with providers_faked(providers), memory_cache() as cache:
        result = search_flights(FlightSearchInput(**FLIGHTS))

    assert isinstance(result, ToolError)
    assert result.code == "RATE_LIMITED"
    assert "duffel" in result.error and "429" in result.error
    assert providers.counts()["duffel"] == 4
    assert cache == {}  # a failure is never kept
    # …which the traveller is told in their own words, with a way to try again
    assert failure_words(result.model_dump()) == "This search is busy right now. Try again in a minute."
    assert can_retry(result.model_dump()) is True


@pytest.mark.parametrize(
    "provider, search",
    [
        ("liteapi", lambda: search_hotels(HotelSearchInput(**HOTELS))),
        ("opentripmap", lambda: get_attractions(BEACHES)),
        ("nominatim", lambda: get_attractions(BEACHES)),  # the place is looked up first
        ("openweather", lambda: get_weather(WEATHER_SOON)),
    ],
)
def test_every_provider_that_keeps_saying_429_is_named_in_a_rate_limited_error(provider, search, no_waiting):
    providers = FakeProviders()
    providers.turn_away[provider] = [429] * 10

    with providers_faked(providers), memory_cache():
        result = search()

    assert isinstance(result, ToolError)
    assert result.code == "RATE_LIMITED"
    assert provider in result.error
    assert providers.counts()[provider] == 4


def test_a_provider_whose_queue_is_full_is_a_rate_limited_search_without_a_request():
    providers = FakeProviders()
    full = MagicMock()
    full.acquire.side_effect = QueueFull("duffel", 75)

    with providers_faked(providers), memory_cache(), patch.object(outbound, "limiter", return_value=full):
        result = search_flights(FlightSearchInput(**FLIGHTS))

    assert result.code == "RATE_LIMITED"
    assert "too many requests are already waiting" in result.error
    assert providers.requests == []  # the provider was not asked at all


def test_a_server_error_keeps_the_code_it_always_had(no_waiting):
    providers = FakeProviders()
    providers.turn_away["duffel"] = [500] * 10

    with providers_faked(providers), memory_cache():
        result = search_flights(FlightSearchInput(**FLIGHTS))

    assert (result.code, result.error) == ("DUFFEL_ERROR", "Duffel API error: HTTP 500")
    assert providers.counts()["duffel"] == 4  # but only after the retries


# ── The server: tools in threads ───────────────────────────────────────────


async def test_running_a_tool_in_a_thread_changes_nothing_a_caller_can_see():
    plain = FastMCP("the tools as plain functions")
    for tool in TOOLS:
        plain.add_tool(tool)

    threaded = {tool.name: tool for tool in await mcp.list_tools()}
    unthreaded = {tool.name: tool for tool in await plain.list_tools()}

    assert list(threaded) == ["search_flights", "search_hotels", "get_attractions", "get_weather", "estimate_budget"]
    for name, tool in threaded.items():
        assert tool.inputSchema == unthreaded[name].inputSchema
        assert tool.outputSchema == unthreaded[name].outputSchema
        assert tool.description == unthreaded[name].description


async def test_two_searches_are_with_their_providers_at_the_same_moment():
    """Each request waits at its provider for the other to arrive. Tools run one after another never meet."""
    together = threading.Barrier(2, timeout=3)
    providers = FakeProviders()
    providers.on_request = lambda _provider: together.wait()

    with memory_cache():
        async with tool_server(providers):
            flights, hotels = await asyncio.gather(
                call_tool("search_flights", FLIGHTS), call_tool("search_hotels", HOTELS)
            )

    assert not isinstance(flights, ToolError), flights
    assert not isinstance(hotels, ToolError), hotels
    assert providers.counts() == {"duffel": 1, "liteapi": 1}
    assert len(flights) == 2 and len(hotels) == 2  # and the lists arrive whole through the real client


async def test_three_agents_searching_at_once_never_see_a_429(no_waiting, caplog):
    """The roadmap's scenario, on the real server: three agents, one provider that allows two requests a window.

    The provider counts in real time and answers 429 to a third request inside
    its window. The limiter is told the same limit over a longer window, and
    really waits. Every agent gets its flights, the provider never says 429,
    and nothing needed a backoff.
    """
    providers = FakeProviders()
    providers.limits["duffel"] = (2, 0.25)
    held: list[float] = []

    def really_wait(seconds: float) -> None:
        held.append(seconds)
        time.sleep(seconds)

    searches = [{**FLIGHTS, "date": (DAY + timedelta(days=n)).isoformat()} for n in range(3)]  # three different ones
    with (
        patch.dict(LIMITS, {"duffel": Limit(2, 0.4)}),
        patch.object(rate_limiter, "_sleep", really_wait),
        memory_cache(),
        caplog.at_level(logging.INFO, logger="src.ai.mcp_server.rate_limiter"),
    ):
        async with tool_server(providers):
            results = await asyncio.gather(*(FlightAgent().run(search) for search in searches))

    assert [result["error"] for result in results] == [None, None, None]
    assert all(len(result["flights"]) == 2 for result in results)  # all three agents complete
    assert providers.answers == [("duffel", 200)] * 3  # zero raw 429s
    assert backoffs(no_waiting) == []  # nothing had to be tried again
    assert len(held) == 1 and 0.3 < held[0] <= 0.44  # the third search waited for the window instead
    assert "[RATE LIMIT] duffel: limit of 2 per 0.44 s reached — waiting" in caplog.text


async def test_three_agents_and_a_forced_429_all_complete(no_waiting):
    """A 429 the limiter could not have prevented — the key is shared, the provider hiccups — is caught and retried."""
    providers = FakeProviders()
    providers.turn_away["duffel"] = [429]

    searches = [{**FLIGHTS, "date": (DAY + timedelta(days=n)).isoformat()} for n in range(3)]
    with memory_cache():
        async with tool_server(providers):
            results = await asyncio.gather(*(FlightAgent().run(search) for search in searches))

    assert [result["error"] for result in results] == [None, None, None]
    assert sorted(status for _, status in providers.answers) == [200, 200, 200, 429]
    (wait,) = backoffs(no_waiting)
    assert 1 <= wait <= 2

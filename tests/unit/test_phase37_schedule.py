"""Phase 37 — a day's clock, and the travel-time tool behind it.

The roadmap's acceptance, and where each is held:

  Dev A  - each activity slot has `start_time` and `end_time`       → test_every_stop_of_a_plan_has_its_times
         - activities > 10 km apart have a travel note              → test_a_stop_more_than_10_km_away_has_a_travel_note
         - a day does not exceed 14 hours of activity               → test_no_day_is_longer_than_14_hours (500 days)
         - "3 itinerary structure tests pass": those three
  Dev B  - the tool returns the travel time for a known route       → here against a stand-in that answers as
           (Fort Aguada to Baga Beach)                                Google does; on Google: docs/phase37_build_log.md
         - cached on the second call ([CACHE HIT] in the logs)      → test_the_second_ask_is_a_cache_hit
"""

import importlib.util
import json
import logging
import random
import re
import sys
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.pdf.document import _stop_row
from app.pdf.plan import TRAVEL_MODES, build_plan
from src.ai import schedule
from src.ai.builder import builder
from src.ai.itinerary import FREE_TIME, SLOTS
from src.ai.mcp_server import outbound
from src.ai.mcp_server.cache import make_cache_key
from src.ai.mcp_server.models import CACHES, TOOL_NAMES, ToolError, TravelTimeInput
from src.ai.mcp_server.tools import ROUTES_FIELDS, ROUTES_MATRIX_URL, TTL_TRAVEL, get_travel_time
from src.ai.orchestrator.orchestrator import OrchestratorAgent
from tests import fakes
from tests.fakes import ATTRACTIONS, HOTELS, FakeProviders, flight, memory_cache, network_stubs, providers_faked

FORT_AGUADA = (15.4925, 73.7735)
BAGA_BEACH = (15.5553, 73.7517)
OLD_GOA = (15.5009, 73.9116)  # the Basilica of Bom Jesus: about 15 km inland from Fort Aguada


def ask(a=FORT_AGUADA, b=BAGA_BEACH, mode="auto-rickshaw") -> TravelTimeInput:
    return TravelTimeInput(origin_lat=a[0], origin_lng=a[1], dest_lat=b[0], dest_lng=b[1], mode=mode)


def stops_of(plan: dict) -> list[dict]:
    return [day[slot] for day in plan["days"] for slot in SLOTS if day.get(slot)]


def minutes(hhmm: str) -> int:
    hours, mins = hhmm.split(":")
    return int(hours) * 60 + int(mins)


# ── The tool ───────────────────────────────────────────────────────────────


def test_without_a_key_the_tool_says_so_and_asks_nobody():
    providers = FakeProviders()
    with (
        providers_faked(providers),
        memory_cache(),
        patch("src.ai.mcp_server.tools.mcp_settings.GOOGLE_MAPS_API_KEY", ""),
    ):
        answer = get_travel_time(ask())
    assert isinstance(answer, ToolError) and answer.code == "API_NOT_CONFIGURED"
    assert providers.requests == []


def test_a_journey_is_one_element_of_googles_route_matrix():
    providers = FakeProviders()
    with providers_faked(providers), memory_cache():
        answer = get_travel_time(ask())
    body, headers = providers.routes[0], providers.route_headers[0]
    assert headers == {"X-Goog-Api-Key": "g", "X-Goog-FieldMask": ROUTES_FIELDS}
    assert body == {
        "origins": [{"waypoint": {"location": {"latLng": {"latitude": 15.4925, "longitude": 73.7735}}}}],
        "destinations": [{"waypoint": {"location": {"latLng": {"latitude": 15.5553, "longitude": 73.7517}}}}],
        "travelMode": "DRIVE",  # an auto-rickshaw is asked as a car: Google has none
    }
    assert "routingPreference" not in body  # no traffic: the Essentials SKU, and a time a day's cache can hold
    assert ROUTES_MATRIX_URL == "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
    # 7.3 km as the crow flies; the stand-in answers 1.3 times that by road at 30 km/h
    assert answer.model_dump() == {"minutes": 20, "km": 9.6, "mode": "auto-rickshaw", "source": "google"}


@pytest.mark.parametrize(("mode", "asked"), [("walk", "WALK"), ("taxi", "DRIVE"), ("auto-rickshaw", "DRIVE")])
def test_each_mode_is_asked_as_google_knows_it(mode, asked):
    providers = FakeProviders()
    with providers_faked(providers), memory_cache():
        get_travel_time(ask(mode=mode))
    assert providers.routes[0]["travelMode"] == asked


def test_the_second_ask_is_a_cache_hit(caplog):
    providers = FakeProviders()
    caplog.set_level(logging.INFO, logger="src.ai.mcp_server.cache")
    with providers_faked(providers), patch("src.ai.mcp_server.cache._get_client") as redis:
        stored: dict = {}
        redis.return_value.get.side_effect = lambda key: stored.get(key)
        redis.return_value.set.side_effect = lambda key, value, ex: stored.__setitem__(key, value)
        first, second = get_travel_time(ask()), get_travel_time(ask())
    assert first == second and len(providers.routes) == 1  # Google was asked once
    lines = [record.getMessage() for record in caplog.records]
    key = make_cache_key(
        "travel", {**{k: round(v, 5) for k, v in ask().model_dump().items() if k != "mode"}, "mode": "auto-rickshaw"}
    )
    assert f"[CACHE MISS] {key}" in lines and f"[CACHE HIT]  {key}" in lines
    assert redis.return_value.set.call_args.kwargs["ex"] == TTL_TRAVEL == 86_400  # a day


def test_the_same_two_places_to_the_metre_are_one_entry():
    providers = FakeProviders()
    with providers_faked(providers), memory_cache() as cache:
        get_travel_time(ask(a=(15.4925001, 73.7735002)))
        get_travel_time(ask())
    assert len(providers.routes) == 1 and len(cache) == 1 and next(iter(cache)).startswith("mcp:travel:")


@pytest.mark.parametrize(
    ("status", "code"), [(403, "API_NOT_AUTHORIZED"), (401, "API_NOT_AUTHORIZED"), (500, "PROVIDER_ERROR")]
)
def test_a_refusal_or_an_error_is_a_tool_error(status, code):
    providers = FakeProviders()
    providers.turn_away["google_routes"] = [status] * 4
    with providers_faked(providers), memory_cache() as cache, patch.object(outbound, "_sleep"):
        answer = get_travel_time(ask())
    assert isinstance(answer, ToolError) and answer.code == code and cache == {}  # nothing cached


def test_no_route_is_a_tool_error_and_not_cached():
    providers = FakeProviders()
    providers.route_condition = "ROUTE_NOT_FOUND"
    with providers_faked(providers), memory_cache() as cache:
        answer = get_travel_time(ask())
    assert isinstance(answer, ToolError) and answer.code == "NO_ROUTE" and cache == {}


def test_coordinates_off_the_earth_are_refused_before_anything_is_asked():
    with pytest.raises(ValueError):
        TravelTimeInput(origin_lat=95, origin_lng=73, dest_lat=15, dest_lng=73)
    with pytest.raises(ValueError):
        ask(mode="helicopter")


def test_the_tool_and_its_cache_are_counted_from_the_start():
    assert "get_travel_time" in TOOL_NAMES and "travel" in CACHES


# ── The estimate ───────────────────────────────────────────────────────────


def test_the_way_is_chosen_by_the_distance():
    assert [schedule.mode_for(km) for km in (0.4, 1.5, 1.6, 10.0, 10.1, 40)] == [
        "walk",
        "walk",
        "auto-rickshaw",
        "auto-rickshaw",
        "taxi",
        "taxi",
    ]


def test_an_estimate_is_the_straight_line_made_a_road_driven_without_traffic():
    km = schedule.straight_km(FORT_AGUADA, BAGA_BEACH)
    assert 7.2 < km < 7.4
    assert schedule.estimate(km, "auto-rickshaw") == {
        "minutes": 20,  # OpenStreetMap's router: 13 minutes for this drive — and 5 to find the auto-rickshaw
        "km": 11.8,
        "mode": "auto-rickshaw",
        "source": "estimate",
    }
    assert schedule.estimate(1.0, "walk")["minutes"] == 22  # 1.6 km of road at 4.5 km/h
    assert schedule.estimate(0, "taxi")["minutes"] == 5  # the taxi still has to be found


# ── The schedule ───────────────────────────────────────────────────────────


def stop(name: str, at: tuple[float, float] | None, category: str = "history") -> dict:
    lat, lng = at if at else (None, None)
    return {"activity": name, "cost": 0.0, "lat": lat, "lng": lng, "category": category}


HOTEL = {"name": "Goa Grand", "cost_per_night": 4500, "lat": 15.5, "lng": 73.8}


def a_day(n: int, hotel=HOTEL, flight_=None, **slots) -> dict:
    return {"day": n, "date": f"2030-01-{9 + n:02d}", "morning": None, "afternoon": None, "evening": None,
            "hotel": hotel, "flight": flight_, **slots}  # fmt: skip


async def no_google(_params: dict) -> None:
    return None


async def test_every_stop_of_a_plan_has_its_times():
    """Roadmap: each activity slot has start_time and end_time — a whole plan, through the planner, on the stubs."""
    with network_stubs(flight_tool=lambda *_: [flight(8_200.0)]):
        trip = {"destination": "Goa", "start_date": "2030-01-10", "end_date": "2030-01-13", "budget": 80_000.0,
                "group_size": 1, "interests": ["history"]}  # fmt: skip
        result = await OrchestratorAgent().run(trip, trip_id=uuid.uuid4())
    stops = stops_of(result["draft_itinerary"])
    assert stops and all(stop["start_time"] and stop["end_time"] for stop in stops)
    for day in result["draft_itinerary"]["days"]:
        for slot in SLOTS:
            if stop_ := day.get(slot):
                opens, closes = schedule.SLOT_WINDOWS[slot]
                assert opens <= minutes(stop_["start_time"]) < minutes(stop_["end_time"]) <= schedule.DAY_END


async def test_a_stop_more_than_10_km_away_has_a_travel_note():
    plan = {"days": [a_day(1, morning=stop("Fort Aguada", FORT_AGUADA), afternoon=stop("Baga Beach", BAGA_BEACH),
                           evening=stop("Basilica of Bom Jesus", OLD_GOA, "spiritual"))]}  # fmt: skip
    await schedule.add_times(plan, no_google)
    morning, afternoon, evening = (plan["days"][0][slot] for slot in SLOTS)
    assert schedule.straight_km(BAGA_BEACH, OLD_GOA) > 10 > schedule.straight_km(FORT_AGUADA, BAGA_BEACH)
    assert "travel" not in afternoon  # 7 km: no note — only its minutes
    assert afternoon["travel_from_previous_minutes"] == 20
    assert evening["travel"] == {
        "from": "Baga Beach",
        "minutes": evening["travel_from_previous_minutes"],
        "km": evening["travel"]["km"],
        "mode": "taxi",
        "source": "estimate",
    }
    assert evening["travel"]["km"] > 10 * schedule.ROAD_FACTOR - 1


async def test_no_day_is_longer_than_14_hours():
    """Roadmap: a day does not exceed 14 hours of activity — on 500 days made at random: places up to 40 km
    from the centre, any kind, any slots filled, a flight landing at any time of the first day."""
    rng = random.Random(37)
    kinds = [*schedule.TYPICAL_MINUTES, "unknown", None]
    for n in range(500):

        def place(i: int) -> dict:
            at = (15.5 + rng.uniform(-0.36, 0.36), 73.85 + rng.uniform(-0.36, 0.36))
            return stop(f"Place {i}", at if rng.random() > 0.05 else None, rng.choice(kinds))

        landing = f"2030-01-10T{rng.randrange(0, 24):02d}:{rng.randrange(0, 60):02d}:00" if rng.random() < 0.5 else None
        landed = {"arrival": landing, "destination": {"code": "GOI", "lat": 15.38, "lng": 73.83}} if landing else None
        if rng.random() < 0.1:  # a free day: free time stands alone, as the builder leaves it (_normalise_free_time)
            slots = {"morning": stop(FREE_TIME, None) | {"category": None}}
        else:
            slots = {slot: place(i) for i, slot in enumerate(SLOTS) if rng.random() < 0.8}
        plan = {"days": [a_day(1, flight_=landed, **slots)]}
        await schedule.add_times(plan, no_google)
        timed = [s for s in (plan["days"][0].get(slot) for slot in SLOTS) if s and s.get("start_time")]
        for s in timed:
            assert schedule.DAY_START <= minutes(s["start_time"]) <= minutes(s["end_time"]) <= schedule.DAY_END, (n, s)
        for before, after in zip(timed, timed[1:]):
            assert minutes(after["start_time"]) >= minutes(before["end_time"]), (n, before, after)  # no overlap
        if timed:
            span = minutes(timed[-1]["end_time"]) - minutes(timed[0]["start_time"])
            assert span <= 14 * 60, (n, span)
        for s in (plan["days"][0].get(slot) for slot in SLOTS):
            if s and not s.get("start_time"):
                assert s.get("no_time_left") and landing, (n, s)  # only a late landing leaves a stop without times


async def test_a_stop_starts_when_the_traveller_can_be_there():
    plan = {
        "days": [
            a_day(1, morning=stop("Fort Aguada", FORT_AGUADA, "beach"), afternoon=stop("Basilica", OLD_GOA, "museum"))
        ]
    }
    await schedule.add_times(plan, no_google)
    morning, afternoon = plan["days"][0]["morning"], plan["days"][0]["afternoon"]
    from_hotel = morning["travel_from_previous_minutes"]
    assert morning["start_time"] == schedule.hhmm(-(-(8 * 60 + from_hotel) // 5) * 5)  # the way from the hotel, first
    assert minutes(morning["end_time"]) - minutes(morning["start_time"]) <= schedule.TYPICAL_MINUTES["beach"]
    assert morning["end_time"] <= "12:00"
    assert afternoon["start_time"] == "13:00"  # the drive fits in the lunch hour
    assert afternoon["end_time"] == "15:00"  # a museum: two hours
    assert all(minutes(s["start_time"]) % 5 == 0 for s in (morning, afternoon))


async def test_the_first_day_starts_an_hour_after_the_flight_lands():
    landed = {"arrival": "2030-01-10T11:20:00", "destination": {"code": "GOI", "lat": 15.38, "lng": 73.83}}
    plan = {
        "days": [a_day(1, flight_=landed, morning=stop("Fort Aguada", FORT_AGUADA), evening=stop("Baga", BAGA_BEACH))]
    }
    await schedule.add_times(plan, no_google)
    morning = plan["days"][0]["morning"]
    assert morning["travel"]["from"] == "the airport"  # 13 km: a taxi, and a note
    assert minutes(morning["start_time"]) >= 11 * 60 + 20 + 60 + morning["travel_from_previous_minutes"]
    assert morning.get("short_visit")  # the morning was mostly the flight
    landed_late = {**landed, "arrival": "2030-01-10T21:10:00"}
    late = {"days": [a_day(1, flight_=landed_late, morning=stop("Fort Aguada", FORT_AGUADA))]}
    await schedule.add_times(late, no_google)
    assert late["days"][0]["morning"].items() >= {"start_time": None, "end_time": None, "no_time_left": True}.items()


async def test_a_day_starts_from_the_hotel_the_traveller_woke_up_in():
    first = {**HOTEL, "name": "North Inn", "lat": 15.6, "lng": 73.74}
    plan = {
        "days": [
            a_day(1, hotel=first, morning=stop("A", FORT_AGUADA)),
            a_day(2, hotel=None, morning=stop("B", OLD_GOA)),
        ]
    }
    await schedule.add_times(plan, no_google)
    assert plan["days"][1]["morning"]["travel"]["from"] == "North Inn"  # the last day has no hotel: the one before


async def test_a_free_day_has_its_window_and_no_journey():
    plan = {"days": [a_day(1, morning=stop(FREE_TIME, None) | {"category": None})]}
    await schedule.add_times(plan, no_google)
    assert (
        plan["days"][0]["morning"].items()
        >= {"start_time": "08:00", "end_time": "12:00", "travel_from_previous_minutes": None}.items()
    )


async def test_googles_answer_is_used_and_said_to_be_googles():
    answers = []

    async def google(params: dict) -> dict:
        answers.append(params)
        return {"minutes": 41, "km": 17.2, "mode": params["mode"], "source": "google"}

    plan = {"days": [a_day(1, morning=stop("Fort Aguada", FORT_AGUADA), afternoon=stop("Basilica", OLD_GOA))]}
    timing = await schedule.add_times(plan, google)
    afternoon = plan["days"][0]["afternoon"]
    assert afternoon["travel"] == {"from": "Fort Aguada", "minutes": 41, "km": 17.2, "mode": "taxi", "source": "google"}
    assert timing == {"legs": 2, "from_google": 2, "far": 1}
    assert answers[1] == {
        "origin_lat": 15.4925,
        "origin_lng": 73.7735,
        "dest_lat": 15.5009,
        "dest_lng": 73.9116,
        "mode": "taxi",
    }


async def test_times_a_model_writes_are_not_listened_to():
    written = stop("Fort Aguada", FORT_AGUADA) | {
        "start_time": "03:00",
        "end_time": "23:59",
        "travel": {"from": "Mars"},
        "short_visit": True,
    }
    plan = {"days": [a_day(1, morning=written)]}
    await schedule.add_times(plan, no_google)
    morning = plan["days"][0]["morning"]
    assert morning["start_time"] >= "08:00" and morning["end_time"] <= "12:00"
    assert "travel" not in morning and "short_visit" not in morning


# ── The builder asks the tool ──────────────────────────────────────────────


async def test_the_builder_asks_the_tool_for_each_journey_and_logs_how_many_google_answered():
    async def google(name: str, params: dict) -> dict:
        assert name == "get_travel_time"
        return {"minutes": 25, "km": 9.0, "mode": params["mode"], "source": "google"}

    with (
        network_stubs(flight_tool=lambda *_: [flight(8_200.0)]),
        patch.object(builder, "call_tool", AsyncMock(side_effect=google)) as tool,
    ):
        result = await builder.ItineraryBuilder().run(
            trip_meta={"destination": "Goa", "start_date": "2030-01-10", "end_date": "2030-01-12", "group_size": 1},
            flights=[flight(8_200.0)],
            hotels=HOTELS,
            attractions=ATTRACTIONS,
        )
    places = [s for s in stops_of(result["draft"]) if s["activity"] != FREE_TIME]
    assert tool.await_count == len(places) == result["travel"]["legs"] == result["travel"]["from_google"]
    assert all(s["travel_from_previous_minutes"] == 25 for s in places)
    assert json.dumps(result["draft"])  # what is saved is plain JSON


async def test_without_the_tool_the_builder_estimates_and_says_so(no_travel_time_tool):
    with patch.object(builder, "_call_llm", fakes.fake_builder_llm):  # the stand-in model: never a real one
        result = await builder.ItineraryBuilder().run(
            trip_meta={"destination": "Goa", "start_date": "2030-01-10", "end_date": "2030-01-12", "group_size": 1},
            flights=[flight(8_200.0)],
            hotels=HOTELS,
            attractions=ATTRACTIONS,
        )
    assert no_travel_time_tool.await_count == result["travel"]["legs"] > 0
    assert result["travel"]["from_google"] == 0
    far = [s["travel"] for s in stops_of(result["draft"]) if s.get("travel")]
    assert far and all(note["source"] == "estimate" for note in far)


def test_the_new_keys_of_a_stop_are_left_out_while_unset():
    slot = builder.ActivitySlot(
        activity="Fort Aguada", start_time="09:00", end_time="11:00", travel_from_previous_minutes=12
    )
    dumped = slot.model_dump()
    assert dumped["start_time"] == "09:00" and dumped["travel_from_previous_minutes"] == 12
    assert {"travel", "short_visit", "no_time_left"}.isdisjoint(dumped)


# ── The PDF, and the page's words for a journey ────────────────────────────


TIMED = {
    "total_cost": 0,
    "days": [
        {
            "day": 1,
            "date": "2030-01-10",
            "morning": {"activity": "Fort Aguada", "cost": 0, "lat": 15.49, "lng": 73.77, "category": "history",
                        "start_time": "10:05", "end_time": "12:00", "travel_from_previous_minutes": 46, "short_visit": True,
                        "travel": {"from": "the airport", "minutes": 46, "km": 18.9, "mode": "taxi", "source": "estimate"}},
            "evening": {"activity": "Basilica", "cost": 0, "lat": 15.5, "lng": 73.91, "start_time": "18:00",
                        "end_time": "19:15", "travel": {"from": "Fort Aguada", "minutes": 75, "km": 22.4, "mode": "taxi",
                        "source": "google"}},
        }
    ],
}  # fmt: skip


def _pdf(structured_data: dict):
    return build_plan(destination="Goa", start_date=date(2030, 1, 10), end_date=date(2030, 1, 10), travellers=1,
                      budget=50_000, interests=None, structured_data=structured_data)  # fmt: skip


def test_the_pdf_prints_each_stops_times_and_the_way_there():
    morning, evening = _pdf(TIMED).days[0].stops
    assert (morning.start_time, morning.end_time, morning.short_visit) == ("10:05", "12:00", True)
    assert morning.travel == "Taxi · about 46 min · 18.9 km from the airport"  # an estimate says so
    assert evening.travel == "Taxi · 1 h 15 min · 22.4 km from Fort Aguada"  # Google's does not
    lines = [paragraph.getPlainText() for paragraph in _stop_row(1, morning)[1]]
    assert lines[0] == "MORNING · 10:05–12:00" and "Taxi · about 46 min · 18.9 km from the airport" in lines
    assert "A short visit — getting here takes much of this part of the day." in lines


def test_an_itinerary_made_before_this_phase_prints_as_it_did():
    old = {
        "total_cost": 0,
        "days": [{"day": 1, "date": "2030-01-10", "morning": {"activity": "Fort Aguada", "lat": 15.49, "lng": 73.77}}],
    }
    (stop_,) = _pdf(old).days[0].stops
    assert (stop_.start_time, stop_.travel, stop_.short_visit) == (None, None, False)
    assert [paragraph.getPlainText() for paragraph in _stop_row(1, stop_)[1]] == ["MORNING", "Fort Aguada"]


def test_the_page_and_the_pdf_name_the_ways_alike():
    source = open("src/frontend/src/lib/schedule.ts", encoding="utf-8").read()
    block = source[source.index("TRAVEL_MODES") : source.index("};", source.index("TRAVEL_MODES"))]
    assert dict(re.findall(r'"?([a-z-]+)"?: "([^"]+)"', block)) == TRAVEL_MODES
    assert '${note.source === "google" ? "" : "about "}' in source  # an estimate is "about", there too


# ── Does the key open Google's Routes API? (scripts/provider_keys.py) ──────


def _provider_keys():
    spec = importlib.util.spec_from_file_location("provider_keys", Path("scripts/provider_keys.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # a dataclass looks its own module up while it is being made
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("status", "accepted"), [(200, True), (400, False), (403, False), (429, True)])
def test_the_key_check_asks_google_one_journey_and_a_400_is_a_refusal(status, accepted):
    keys = _provider_keys()
    (question,) = [
        q for q in keys.questions({"GOOGLE_MAPS_API_KEY": "AIza-test"}) if q.setting == "GOOGLE_MAPS_API_KEY"
    ]
    sent = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status, json=[{"condition": "ROUTE_EXISTS", "duration": "780s"}] if status == 200 else {})

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        ok, said = keys.ask(question, client)
    body = json.loads(sent[0].content)
    assert sent[0].method == "POST" and sent[0].headers["X-Goog-Api-Key"] == "AIza-test"
    assert len(body["origins"]) == len(body["destinations"]) == 1  # one element of the free 10,000 a month
    assert ok is accepted and "AIza-test" not in said


# ── The builder is told when the first day can begin (DECISIONS #340) ──────


def _lands(at: str) -> list[dict]:
    return [{"price_inr": 8_200.0, "arrival": at, "destination": {"code": "GOI", "lat": 15.38, "lng": 73.83}}]


@pytest.mark.parametrize(
    ("arrival", "said"),
    [
        ("2030-01-10T06:30:00", ""),  # landed before the day begins: nothing to say
        ("2030-01-11T14:05:00", ""),  # lands on another day than the first: not this rule's
        ("2030-01-10T14:05:00", "The flight lands at 14:05 on 2030-01-10: the traveller is free from about 15:05."),
        ("2030-01-10T22:42:00", 'too late for anything that day: day 1 gets only the one "Explore the area" slot.'),
    ],
)
def test_the_builder_is_told_when_the_first_day_can_begin(arrival, said):
    line = builder._landing_line("2030-01-10", _lands(arrival))
    assert (said in line) if said else line == ""
    prompt = builder._build_user_prompt(
        {"destination": "Goa", "start_date": "2030-01-10", "end_date": "2030-01-12"},
        _lands(arrival),
        HOTELS,
        ATTRACTIONS,
    )
    assert (line in prompt) and prompt.count("The flight lands") == (1 if said else 0)


def test_the_line_speaks_of_the_flight_the_plan_takes_the_cheapest():
    flights = _lands("2030-01-10T22:42:00") + [{**_lands("2030-01-10T09:00:00")[0], "price_inr": 4_000.0}]
    assert "lands at 09:00" in builder._landing_line("2030-01-10", flights)


def test_a_late_landings_free_day_says_why_it_is_empty():
    late = {
        "total_cost": 0,
        "days": [{"day": 1, "date": "2030-01-10", "morning": {"activity": FREE_TIME, "no_time_left": True}}],
    }
    (free,) = _pdf(late).days[0].stops
    assert free.no_time_left and "You arrive late — nothing is planned for today." in [
        paragraph.getPlainText() for paragraph in _stop_row(1, free)[1]
    ]
    source = open("src/frontend/src/components/DayCard.tsx", encoding="utf-8").read()
    assert "You arrive late — nothing is planned for today." in source  # the page says it in the same words

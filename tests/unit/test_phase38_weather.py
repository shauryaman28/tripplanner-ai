"""Phase 38 — the weather a plan is made for.

The roadmap's acceptance, and where each is held:

  Dev A  - a day with a "Rainy" forecast has at least one indoor activity  → test_a_rainy_day_gets_an_indoor_stop…
         - a "Sunny" day's beach and outdoor places are not excluded       → …and_a_sunny_days_beach_stays
         - tested with a known rainy-day forecast fixture                  → RAINY_DAY_TWO
  Dev B  - the icon for all three condition types; the climate badge on    → e2e/weather.spec.ts (the page);
           day 6+                                                           here, what the page is given
"""

import io
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pypdf import PdfReader

from app.api.routes.trips import _AGENT_LABELS, _run_detail
from app.pdf.document import build_pdf
from app.pdf.plan import WEATHER_LABELS, build_plan
from src.ai import weather
from src.ai.builder import builder
from src.ai.itinerary import FREE_TIME, SLOTS
from src.ai.mcp_server.models import DayForecast, WeatherInput
from src.ai.mcp_server.tools import _climate_forecast, _owm_days, get_weather
from src.ai.orchestrator import orchestrator
from src.ai.orchestrator.orchestrator import OrchestratorAgent
from tests import fakes
from tests.fakes import ATTRACTIONS, HOTELS, flight, network_stubs

MUSEUM = {
    "name": "Goa State Museum",
    "category": "museum",
    "rating": 3.0,
    "description": "Museum.",
    "lat": 15.497,
    "lng": 73.828,
}
PLACES = [*ATTRACTIONS, MUSEUM]  # Fort Aguada (history), Baga Beach (beach), the Basilica (spiritual), a museum
TRIP_META = {"destination": "Goa", "start_date": "2030-01-10", "end_date": "2030-01-12", "group_size": 1}


def day_of(dates: list[str], rainy: set[int]) -> list[dict]:
    return [
        {
            "date": d,
            "condition": "Rain" if n + 1 in rainy else "Clear",
            "temp_high_c": 30.0,
            "temp_low_c": 22.0,
            "source": "forecast",
        }
        for n, d in enumerate(dates)
    ]


DATES = ["2030-01-10", "2030-01-11", "2030-01-12"]
RAINY_DAY_TWO = day_of(DATES, {2})  # the roadmap's "known rainy-day forecast fixture"


def stops_of(day: dict) -> list[dict]:
    return [day[slot] for slot in SLOTS if day.get(slot)]


# ── The weather tool ───────────────────────────────────────────────────────

IST = 19_800  # OWM's city.timezone for India, in seconds east of UTC


def owm(readings: list[tuple[date, int, str]], high: float = 31.0, low: float = 24.0) -> dict:
    """An OWM 3-hourly forecast: (local date, local hour, main) each — at the place's own clock (IST)."""
    items = []
    for day, hour, main in readings:
        at = datetime(day.year, day.month, day.day, hour, tzinfo=timezone(timedelta(seconds=IST)))
        items.append(
            {"dt": int(at.timestamp()), "weather": [{"main": main}], "main": {"temp_max": high, "temp_min": low}}
        )
    return {"city": {"timezone": IST}, "list": items}


def every_three_hours(day: date, main: str, wet_hours: tuple[int, ...] = ()) -> list[tuple[date, int, str]]:
    return [(day, hour, "Rain" if hour in wet_hours else main) for hour in range(0, 24, 3)]


def test_six_hours_of_daytime_rain_make_a_rainy_day_and_rain_at_night_does_not():
    d = date(2030, 1, 10)
    days = _owm_days(
        owm(
            every_three_hours(d, "Clouds", wet_hours=(9, 12))  # two daytime readings: rainy
            + every_three_hours(d + timedelta(days=1), "Clear", wet_hours=(0, 3))  # rain at night only: clear
            + every_three_hours(d + timedelta(days=2), "Clear", wet_hours=(15,))  # three hours of it: clear
        )
    )
    assert [days[str(d + timedelta(days=n))].condition for n in range(3)] == ["Rain", "Clear", "Clear"]


def test_a_thunderstorm_is_the_wetter_word():
    d = date(2030, 1, 10)
    readings = [(d, 9, "Rain"), (d, 12, "Thunderstorm"), (d, 15, "Thunderstorm"), (d, 18, "Rain")]
    assert _owm_days(owm(readings))[str(d)].condition == "Thunderstorm"


def test_a_day_is_the_places_own_not_the_servers():
    d = date(2030, 1, 10)
    # 02:00 in Goa is 20:30 UTC the day before: it is Goa's tenth
    days = _owm_days(owm([(d, 2, "Clear"), (d, 11, "Clear")]))
    assert list(days) == [str(d)]


def test_a_trip_gets_its_own_days_the_forecast_and_then_the_climate():
    """The trip starts tomorrow and lasts a week: OWM's forecast reaches its first days, the climate the rest
    — dated the trip's days, not counted from today."""
    start = date.today() + timedelta(days=1)
    answer = owm([r for n in range(5) for r in every_three_hours(date.today() + timedelta(days=n), "Clear")])
    response = MagicMock(json=MagicMock(return_value=answer), raise_for_status=MagicMock())
    with (
        patch("src.ai.mcp_server.tools.mcp_settings", MagicMock(OPENWEATHER_API_KEY="k")),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools.httpx.get", return_value=response),
    ):
        days = get_weather(WeatherInput(destination="Goa", date_range=f"{start} to {start + timedelta(days=6)}"))
    assert [d.date for d in days] == [str(start + timedelta(days=n)) for n in range(7)]
    assert [d.source for d in days] == ["forecast"] * 4 + ["climate"] * 3
    assert all(d.condition.endswith("(climate estimate)") for d in days[4:])


def test_a_place_the_climate_table_does_not_know_gets_no_guess():
    assert _climate_forecast("Leh", date(2030, 1, 10), 3) == []  # it was "Partly Cloudy", 32° — in January
    goa = _climate_forecast("Goa", date(2030, 7, 10), 2)
    assert [(d.condition, d.source) for d in goa] == [("Rainy (climate estimate)", "climate")] * 2


def test_a_fortnight_is_answered_whole():
    start = date.today() + timedelta(days=30)
    with (
        patch("src.ai.mcp_server.tools.mcp_settings", MagicMock(OPENWEATHER_API_KEY="k")),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
    ):
        days = get_weather(WeatherInput(destination="Goa", date_range=f"{start} to {start + timedelta(days=14)}"))
    assert len(days) == 15 and all(isinstance(d, DayForecast) for d in days)


# ── What the weather means ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("condition", "kind"),
    [
        ("Clear", "sunny"),
        ("Sunny (climate estimate)", "sunny"),
        ("Rain", "rainy"),
        ("Drizzle", "rainy"),
        ("Thunderstorm", "rainy"),
        ("Rainy (climate estimate)", "rainy"),
        ("Clouds", "cloudy"),
        ("Partly Cloudy (climate estimate)", "cloudy"),
        ("Haze", "cloudy"),
        ("Snow", "snowy"),
        (None, "cloudy"),
    ],
)
def test_every_condition_is_one_of_four_kinds(condition, kind):
    assert weather.kind(condition) == kind


def test_a_museum_a_meal_a_shop_a_show_and_a_temple_are_indoors():
    assert {
        category: weather.setting(category)
        for category in ("museum", "food", "shopping", "culture", "spiritual", "nightlife")
    } == dict.fromkeys(("museum", "food", "shopping", "culture", "spiritual", "nightlife"), "indoor")
    assert {weather.setting(c) for c in ("beach", "history", "nature", "adventure", "sightseeing", None)} == {"outdoor"}


def test_the_model_is_told_the_weather_only_when_a_day_is_rainy():
    dry = weather.by_date(day_of(DATES, set()))
    wet = weather.by_date(RAINY_DAY_TWO)
    assert weather.block(DATES, dry) == ""
    told = weather.block(DATES, wet)
    assert '[{"date": "2030-01-10", "weather": "Clear"}, {"date": "2030-01-11", "weather": "Rain"}' in told
    assert "On a rainy day, use at least one indoor attraction" in told
    assert "outdoor places are not to be avoided on them" in told  # a sunny day is planned as usual


def test_the_builders_prompt_is_unchanged_for_a_dry_trip_and_has_the_weather_for_a_wet_one():
    plain = builder._build_user_prompt(TRIP_META, [flight(8_200.0)], HOTELS, PLACES)
    assert (
        builder._build_user_prompt(TRIP_META, [flight(8_200.0)], HOTELS, PLACES, weather.by_date(day_of(DATES, set())))
        == plain
    )
    wet = builder._build_user_prompt(TRIP_META, [flight(8_200.0)], HOTELS, PLACES, weather.by_date(RAINY_DAY_TWO))
    shown = json.loads(wet.split("Available attractions (JSON): ", 1)[1].split("\n", 1)[0])
    assert {a["name"]: a["setting"] for a in shown} == {
        "Fort Aguada": "outdoor",
        "Baga Beach": "outdoor",
        "Basilica of Bom Jesus": "indoor",
        "Goa State Museum": "indoor",
    }
    assert "Weather by day (JSON" in wet and '"setting"' not in plain


# ── The repair ─────────────────────────────────────────────────────────────


def a_plan(*days: dict) -> dict:
    return {"days": list(days), "total_cost": 900.0}


def a_day(n: int, **slots) -> dict:
    return {"day": n, "date": DATES[n - 1], "morning": None, "afternoon": None, "evening": None, **slots}


def stop(name: str, cost: float = 0.0, **more) -> dict:
    return {"activity": name, "cost": cost, **more}


def test_a_rainy_day_with_an_indoor_stop_is_left_as_it_is():
    plan = a_plan(
        a_day(1),
        a_day(2, morning=stop("Fort Aguada"), afternoon=stop("Baga Beach"), evening=stop("Basilica of Bom Jesus")),
    )
    assert weather.repair(plan, PLACES, weather.by_date(RAINY_DAY_TWO)) == []


def test_a_rainy_day_with_a_slot_free_gets_an_indoor_place_in_it_and_nothing_is_given_up():
    plan = a_plan(
        a_day(1),
        a_day(2, morning=stop("Fort Aguada"), afternoon=stop("Baga Beach", 300.0)),
        a_day(3, morning=stop("Basilica of Bom Jesus")),
    )
    done = weather.repair(plan, PLACES, weather.by_date(RAINY_DAY_TWO))
    assert done == ["day 2 evening: Goa State Museum (indoor)"]
    assert plan["days"][1]["evening"]["activity"] == "Goa State Museum"
    assert plan["days"][1]["afternoon"]["activity"] == "Baga Beach" and plan["total_cost"] == 900.0


def test_with_no_slot_free_the_last_outdoor_stop_gives_way_and_its_cost_goes_with_it():
    plan = a_plan(
        a_day(1), a_day(2, morning=stop("Fort Aguada"), afternoon=stop("Baga Beach"), evening=stop("Anjuna", 300.0))
    )
    places = [*PLACES, {"name": "Anjuna", "category": "beach"}]
    done = weather.repair(plan, places, weather.by_date(RAINY_DAY_TWO))
    assert done == ["day 2 evening: Basilica of Bom Jesus (indoor) in place of Anjuna"]
    assert plan["days"][1]["evening"] == {"activity": "Basilica of Bom Jesus", "cost": 0.0, "lat": None, "lng": None}
    assert plan["total_cost"] == 600.0


def test_a_rainy_day_with_no_indoor_place_left_says_so():
    plan = a_plan(a_day(1), a_day(2, morning=stop("Fort Aguada"), afternoon=stop("Baga Beach")))
    outdoors_only = [a for a in PLACES if weather.setting(a["category"]) == "outdoor"]
    done = weather.repair(plan, outdoors_only, weather.by_date(RAINY_DAY_TWO))
    assert plan["days"][1]["weather_note"] == weather.NOTE and done == ["day 2: rainy, and no indoor place to give it"]
    assert "weather_note" not in plan["days"][0]  # the dry day says nothing


def test_a_free_rainy_day_gets_an_indoor_place():
    plan = a_plan(a_day(1), a_day(2, morning=stop(FREE_TIME)))
    weather.repair(plan, PLACES, weather.by_date(RAINY_DAY_TWO))
    assert (
        weather.setting(next(a["category"] for a in PLACES if a["name"] == plan["days"][1]["morning"]["activity"]))
        == "indoor"
    )


def test_on_a_group_trip_a_stop_gives_way_only_to_a_place_for_the_same_travellers():
    asha_only = stop("Baga Beach", suits=["Asha"])
    plan = a_plan(
        a_day(1),
        a_day(
            2, morning=stop("Fort Aguada", suits=["Ben"]), afternoon=stop("Anjuna", suits=["Ben"]), evening=asha_only
        ),
    )
    places = [*PLACES[:2], {"name": "Anjuna", "category": "beach", "suits": ["Ben"]},
              {"name": "Ben's museum", "category": "museum", "suits": ["Ben"]}]  # fmt: skip
    weather.repair(plan, places, weather.by_date(RAINY_DAY_TWO))
    assert plan["days"][1]["evening"] is asha_only  # Asha's only stop stays: the museum is not for her
    assert plan["days"][1]["afternoon"]["activity"] == "Ben's museum"  # one of Ben's gives way instead


def test_each_day_carries_its_weather_and_says_when_it_is_an_estimate():
    forecast = day_of(DATES[:2], {2}) + [
        {
            "date": DATES[2],
            "condition": "Sunny (climate estimate)",
            "temp_high_c": 31.0,
            "temp_low_c": 23.0,
            "source": "climate",
        }
    ]
    plan = a_plan(a_day(1), a_day(2), a_day(3), {"day": 4, "date": "2030-01-13"})
    weather.attach(plan, weather.by_date(forecast))
    assert plan["days"][1]["weather"] == {
        "condition": "Rain",
        "kind": "rainy",
        "temp_high_c": 30.0,
        "temp_low_c": 22.0,
        "estimate": False,
    }
    assert plan["days"][2]["weather"] | {} == {
        "condition": "Sunny",
        "kind": "sunny",
        "temp_high_c": 31.0,
        "temp_low_c": 23.0,
        "estimate": True,
    }
    assert "weather" not in plan["days"][3]  # a day the tool said nothing of


# ── Through the builder: the roadmap's fixture ─────────────────────────────


async def test_a_rainy_day_gets_an_indoor_stop_and_a_sunny_days_beach_stays():
    """The roadmap's fixture, through the builder. The stand-in model ignores the weather — as a real one may —
    and fills two slots a day from the top of the list: six outdoor places for three days, the museum left
    over. Rain on day 1: code gives day 1 the museum. The sunny days keep their beaches."""
    beaches = [{"name": f"Beach {n}", "category": "beach", "rating": 2.0, "description": "Beach.", "lat": 15.5 + n / 100,
                "lng": 73.76} for n in range(4)]  # fmt: skip
    places = [ATTRACTIONS[0], ATTRACTIONS[1], *beaches, MUSEUM]
    rain_first = day_of(DATES, {1})
    with patch.object(builder, "_call_llm", fakes.fake_builder_llm):
        result = await builder.ItineraryBuilder().run(
            trip_meta=TRIP_META, flights=[flight(8_200.0)], hotels=HOTELS, attractions=places, forecast=rain_first
        )
    days = result["draft"]["days"]
    assert [s["activity"] for s in stops_of(days[0])] == ["Fort Aguada", "Baga Beach", "Goa State Museum"]
    assert days[0]["weather"]["kind"] == "rainy" and [d["weather"]["kind"] for d in days[1:]] == ["sunny", "sunny"]
    assert [s["activity"] for s in stops_of(days[1])] == ["Beach 0", "Beach 1"]  # sunny: the beaches stay
    hotels = sum(d["hotel"]["cost_per_night"] for d in days if d.get("hotel"))
    assert result["draft"]["total_cost"] == 8_200.0 + hotels  # the budget still adds up

    with patch.object(builder, "_call_llm", fakes.fake_builder_llm):
        sunny = await builder.ItineraryBuilder().run(
            trip_meta=TRIP_META,
            flights=[flight(8_200.0)],
            hotels=HOTELS,
            attractions=places,
            forecast=day_of(DATES, set()),
        )
    assert "Goa State Museum" not in [s["activity"] for d in sunny["draft"]["days"] for s in stops_of(d)]


async def test_weather_a_model_writes_on_a_day_is_not_listened_to():
    async def says_it_snows(system, user_prompt, on_token=None):
        plan = json.loads(await fakes.fake_builder_llm(system, user_prompt))
        plan["days"][0] |= {"weather": {"kind": "snowy"}, "weather_note": "Snow!"}
        return json.dumps(plan)

    with patch.object(builder, "_call_llm", says_it_snows):
        result = await builder.ItineraryBuilder().run(
            trip_meta=TRIP_META, flights=[flight(8_200.0)], hotels=HOTELS, attractions=PLACES
        )
    assert {"weather", "weather_note"}.isdisjoint(result["draft"]["days"][0])


# ── Through the orchestrator ───────────────────────────────────────────────


async def test_the_weather_is_read_beside_the_searches_and_logged():
    tool = AsyncMock(return_value=RAINY_DAY_TWO)
    with patch.object(orchestrator, "call_tool", tool), patch.object(orchestrator, "_log", AsyncMock()) as log:
        found = await orchestrator._weather({"destination": " Goa ", "start_date": DATES[0], "end_date": DATES[2]})
    tool.assert_awaited_once_with("get_weather", {"destination": "Goa", "date_range": "2030-01-10 to 2030-01-12"})
    assert found == {"weather": RAINY_DAY_TWO}
    assert log.await_args.args[1] == "weather" and log.await_args.kwargs["output"] | {} == {
        "days": 3,
        "rainy": ["2030-01-11"],
        "estimated": 0,
        "error": None,
    }


async def test_no_weather_is_no_plan_lost(no_weather_tool):
    with patch.object(orchestrator, "_log", AsyncMock()) as log:
        assert await orchestrator._weather({"destination": "Goa", "start_date": DATES[0], "end_date": DATES[2]}) == {
            "weather": None
        }
    assert (
        log.await_args.kwargs["status"] == "failed"
        and log.await_args.kwargs["output"]["error"]["code"] == "API_NOT_CONFIGURED"
    )
    assert await orchestrator._weather({"weather": RAINY_DAY_TWO}) == {}  # a plan that has it keeps it


async def test_a_plan_on_the_stubs_has_its_weather_and_its_rainy_day_an_indoor_place():
    with network_stubs(flight_tool=lambda *_: [flight(8_200.0)]) as tools:
        tools["activities"].return_value = PLACES
        trip = {"destination": "Alleppey", "start_date": "2030-01-10", "end_date": "2030-01-12", "budget": 80_000.0,
                "group_size": 1, "interests": ["history"]}  # fmt: skip
        result = await OrchestratorAgent().run(trip, trip_id=uuid.uuid4())
    days = result["draft_itinerary"]["days"]
    assert [d["weather"]["kind"] for d in days] == ["sunny", "rainy", "sunny"]  # fakes.RAINY_DAYS: Alleppey, day 2
    categories = {a["name"]: a["category"] for a in PLACES}
    assert any(
        weather.setting(categories[s["activity"]]) == "indoor" for s in stops_of(days[1]) if s["activity"] in categories
    )


# ── The PDF, the timeline, and the page's words ────────────────────────────


def test_the_pdf_prints_each_days_weather_its_estimate_and_its_note():
    rainy = {"condition": "Rain", "kind": "rainy", "temp_high_c": 28.6, "temp_low_c": 23.1, "estimate": False}
    typical = {"condition": "Sunny", "kind": "sunny", "temp_high_c": 32, "temp_low_c": 23, "estimate": True}
    data = {"total_cost": 0, "days": [
        {"day": 1, "date": "2030-01-10", "weather": rainy, "weather_note": weather.NOTE, "morning": {"activity": FREE_TIME}},
        {"day": 2, "date": "2030-01-11", "weather": typical, "morning": {"activity": FREE_TIME}},
        {"day": 3, "date": "2030-01-12", "morning": {"activity": FREE_TIME}},
    ]}  # fmt: skip
    plan = build_plan(destination="Goa", start_date=date(2030, 1, 10), end_date=date(2030, 1, 12), travellers=1,
                      budget=None, interests=None, structured_data=data)  # fmt: skip
    assert [(d.weather, d.weather_estimate) for d in plan.days] == [
        ("Rain · 29° / 23°", False),
        ("Sunny · 32° / 23°", True),
        (None, False),
    ]
    assert plan.days[0].weather_note == weather.NOTE

    text = " ".join(" ".join(page.extract_text().split()) for page in PdfReader(io.BytesIO(build_pdf(plan))).pages)
    assert "Rain · 29° / 23°" in text and weather.NOTE in text
    assert "Sunny · 32° / 23° (climate estimate)" in text


def test_the_page_and_the_pdf_name_the_weather_alike():
    source = open("src/frontend/src/lib/weather.ts", encoding="utf-8").read()
    block = source[source.index("WEATHER_LABELS: Record") : source.index("};", source.index("WEATHER_LABELS: Record"))]
    assert dict(re.findall(r"(\w+): \"([^\"]+)\"", block)) == WEATHER_LABELS
    assert (
        set(WEATHER_LABELS)
        == {"sunny", "cloudy", "rainy", "snowy"}
        == {weather.kind(c) for c in ("Clear", "Clouds", "Rain", "Snow")}
    )


def test_the_timeline_says_what_the_weather_was():
    assert _AGENT_LABELS["weather"] == "Read the weather for the trip"
    assert _run_detail("weather", {"days": 3, "rainy": ["2030-01-11"], "estimated": 0, "error": None}, "completed") == {
        "days": 3,
        "rainy": ["2030-01-11"],
        "estimated": 0,
    }

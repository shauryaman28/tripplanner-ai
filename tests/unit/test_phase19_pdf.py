"""Phase 19 — PDF export.

  formatting   money, dates and place wording read the same as on the trip page
  plan         what the pages say: stops, pins, the cost split, the budget note, the file name
  static map   where things land on the picture, the tiles it asks for, every way it can fail
  document     the PDF is built for real and read back: four sections, the map image, ₹
  export       the map is the only part allowed to be missing
  route        GET /trips/{id}/export/pdf — headers, auth, 404s, a tile server that is down

No network and no Docker: tiles come from tests/fakes.fake_tile, and the PDF is
parsed with pypdf.
"""

import asyncio
import base64
import io
import logging
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pypdf import PdfReader

from app.models.itinerary import Itinerary
from app.models.trip import Trip
from app.pdf import MAP_INCLUDED, MAP_NONE, MAP_UNAVAILABLE, NothingToExport, build_plan, export_itinerary
from app.pdf import static_map as static_map_module
from app.pdf.document import build_pdf
from app.pdf.flowables import Card
from app.pdf.formatting import (
    category_label,
    describe_rating,
    format_clock,
    format_date_range,
    format_duration,
    format_inr,
    format_weekday,
    plural,
)
from app.pdf.plan import (
    budget_note,
    export_filename,
    flight_facts,
    flight_route,
    map_features,
    nights_stayed,
    stay_facts,
    stay_totals,
    stop_facts,
)
from app.pdf.static_map import (
    HEIGHT,
    MAX_ZOOM,
    PADDING,
    PIN_SIZE,
    SCALE,
    SINGLE_POINT_ZOOM,
    TILE_TTL,
    WIDTH,
    MapPin,
    MapRoute,
    StaticMapError,
    choose_view,
    project,
    render_static_map,
    spread,
    tile_grid,
    tile_url,
)
from app.pdf.theme import DAY_COLORS, HOTEL_STYLE, day_style
from src.ai.builder.builder import build_itinerary
from src.ai.itinerary import FREE_TIME
from tests.fakes import ATTRACTIONS, HOTELS, fake_builder_llm, fake_tile, flight, map_tile

REPO = Path(__file__).resolve().parents[2]
START = date(2027, 12, 10)
TILES = "https://tiles.example/{z}/{x}/{y}.png"

_FORT, _BEACH, _BASILICA = ATTRACTIONS
_HOTEL = {
    "name": "Goa Grand",
    "cost_per_night": 4500.0,
    "stars": 4,
    "rating": 8.4,
    "address": "Calangute",
    "lat": 15.544,
    "lng": 73.755,
}


def _slot(attraction: dict, cost: float = 0.0) -> dict:
    return {
        "activity": attraction["name"],
        "cost": cost,
        **{k: attraction[k] for k in ("lat", "lng", "category", "rating")},
    }


# The itinerary the stub pipeline saves for a three-day Goa trip: ₹8,200 flights + 2 nights at ₹4,500.
GOA = {
    "days": [
        {
            "day": 1,
            "date": "2027-12-10",
            "morning": _slot(_FORT),
            "afternoon": _slot(_BEACH),
            "evening": None,
            "hotel": _HOTEL,
            "flight": flight(8200.0, day="2027-12-10"),
        },
        {
            "day": 2,
            "date": "2027-12-11",
            "morning": _slot(_BASILICA),
            "afternoon": None,
            "evening": None,
            "hotel": _HOTEL,
            "flight": None,
        },
        {
            "day": 3,
            "date": "2027-12-12",
            "morning": {"activity": FREE_TIME, "cost": 0.0, "lat": None, "lng": None},
            "afternoon": None,
            "evening": None,
            "hotel": None,
            "flight": None,
        },
    ],
    "total_cost": 17200.0,
    "currency": "INR",
}


def _plan(data: object = GOA, **overrides):
    fields = {
        "destination": "Goa",
        "start_date": START,
        "end_date": START + timedelta(days=2),
        "travellers": 2,
        "budget": 50_000.0,
        "interests": ["beach", "food"],
        "structured_data": data,
    }
    return build_plan(**{**fields, **overrides})


def _pages(pdf: bytes) -> list[str]:
    """The text of each page, with runs of whitespace collapsed."""
    return [" ".join(page.extract_text().split()) for page in PdfReader(io.BytesIO(pdf)).pages]


def _page_numbers(pages: list[str]) -> list[str]:
    """What the foot of each page after the cover says, e.g. ["2 of 4", "3 of 4", "4 of 4"]."""
    return [re.search(r"Page (\d+ of \d+)", page).group(1) for page in pages[1:]]


def _images(pdf: bytes) -> list[int]:
    """How many pictures each page shows."""
    return [len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages]


class DictCache:
    """What the route passes as the tile cache: Redis's async get / set, backed by a dict."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key], self.ttl[key] = value, ex


@pytest.fixture
def tiles():
    """Tile downloads answered by a plain tile; yields the mock so a test can count or break them."""
    with patch("app.pdf.static_map._download_tile", AsyncMock(side_effect=fake_tile)) as download:
        yield download


# ── Formatting ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (17_200, "₹17,200"),
        (150_000, "₹1,50,000"),  # lakhs: Indian grouping, as Intl.NumberFormat("en-IN") prints it
        (12_345_678, "₹1,23,45,678"),
        (500, "₹500"),
        (0, "₹0"),
        (14_808.24, "₹14,808"),
        (1_851.5, "₹1,852"),  # a half rounds up, as Math.round does
        (-250, "-₹250"),
    ],
)
def test_format_inr_matches_the_trip_page(amount, text):
    assert format_inr(amount) == text


def test_dates_read_as_on_the_trip_page():
    # the three examples in lib/format.ts
    assert format_date_range(date(2026, 11, 16), date(2026, 11, 20)) == "16 – 20 Nov 2026"
    assert format_date_range(date(2026, 11, 28), date(2026, 12, 2)) == "28 Nov – 2 Dec 2026"
    assert format_date_range(date(2026, 12, 30), date(2027, 1, 3)) == "30 Dec 2026 – 3 Jan 2027"
    assert format_weekday("2026-11-16") == "Mon, 16 Nov"
    assert format_weekday("someday") == "someday"  # not a date: shown as written, never a crash
    assert format_clock("2026-11-16T11:36:00") == "11:36"
    assert format_clock(None) is None
    assert [format_duration(m) for m in (155, 120, 45, 0, None)] == ["2h 35m", "2h", "45m", None, None]
    assert (plural(1, "night"), plural(3, "night"), plural(0, "stop")) == ("1 night", "3 nights", "0 stops")


@pytest.mark.parametrize(
    ("rating", "expected"),
    [
        (2.0, ("Popular", False)),
        (3.0, ("Top attraction", False)),
        (1.0, ("Worth a stop", False)),
        (6.0, ("Popular", True)),  # 5–7 is the same scale on a heritage site
        (7.0, ("Top attraction", True)),
        (0.0, None),  # unrated: nothing is shown, and nothing is invented
        (None, None),
    ],
)
def test_rating_is_popularity_in_words(rating, expected):
    assert describe_rating(rating) == expected


def test_category_labels_are_the_frontends():
    """The PDF and the day cards must call a place the same thing."""
    source = (REPO / "src/frontend/src/lib/places.ts").read_text()
    frontend = dict(re.findall(r'^\s*(\w+):\s*\{ label: "([^"]+)"', source, flags=re.MULTILINE))
    assert frontend and {key: category_label(key) for key in frontend} == frontend
    assert category_label("lighthouse") == "Lighthouse"  # an unknown category is capitalised, as the page does
    assert category_label(None) is None


def test_day_colours_are_the_web_maps():
    """A day is one colour on the page, on the web map and on paper — the palette is validated as a set (#81)."""
    source = (REPO / "src/frontend/src/lib/map.ts").read_text()
    web_palette = re.findall(r"#[0-9a-f]{6}", re.search(r"DAY_COLORS = \[(.*?)\]", source).group(1))
    assert list(DAY_COLORS) == web_palette
    light = set(re.findall(r"#[0-9a-f]{6}", re.search(r"LIGHT_FILLS = new Set<string>\(\[(.*?)\]", source).group(1)))
    assert {colour for colour in DAY_COLORS if day_style(DAY_COLORS.index(colour) + 1).ink != "#ffffff"} == light
    assert HOTEL_STYLE.color in source
    assert f'FREE_TIME = "{FREE_TIME}"' in source
    # past the last colour the hues repeat with another shape, never a new hue
    assert [day_style(day).shape for day in (1, 7, 8, 14, 15, 21, 22)] == [
        "circle",
        "circle",
        "square",
        "square",
        "diamond",
        "diamond",
        "circle",
    ]
    assert day_style(8).color == day_style(1).color


# ── Plan ───────────────────────────────────────────────────────────────────


def test_plan_shows_the_days_as_the_page_does():
    plan = _plan()
    assert (plan.destination, plan.nights, plan.travellers, plan.interests) == ("Goa", 2, 2, ("beach", "food"))
    assert [[stop.name for stop in day.stops] for day in plan.days] == [
        ["Fort Aguada", "Baga Beach"],
        ["Basilica of Bom Jesus"],
        [FREE_TIME],
    ]
    # a pin's number is its place in the day
    assert [[stop.order for stop in day.stops] for day in plan.days] == [[1, 2], [1], [None]]
    assert plan.days[2].stops[0].free_time and plan.days[2].stay is None
    assert [day.cost for day in plan.days] == [4500, 4500, 0]


def test_costs_split_like_the_cost_card():
    costs = _plan().costs
    assert (costs.flights, costs.stay, costs.activities, costs.total, costs.unexplained) == (8200, 9000, 0, 17200, 0)


def test_an_itinerary_saved_before_the_flight_was_attached_carries_it_in_the_total():
    legacy = {**GOA, "days": [{**day, "flight": None} for day in GOA["days"]]}
    costs = _plan(legacy).costs
    assert costs.flights == 8200 and costs.unexplained == 0
    assert _plan(legacy).flight is None


def test_a_total_that_is_not_the_sum_of_its_parts_is_printed_with_the_difference():
    """The builder's total may be up to ₹500 off its parts; the ledger still has to add up."""
    costs = _plan({**GOA, "total_cost": 17_450.0}).costs
    assert costs.total == 17_450 and costs.unexplained == 250


def test_missing_total_falls_back_to_the_saved_column_then_to_the_parts():
    no_total = {"days": GOA["days"]}
    assert _plan(no_total, saved_total=17_200.0).costs.total == 17_200
    assert _plan(no_total).costs.total == 8200 + 9000


def test_free_time_is_said_once_and_never_beside_a_real_stop():
    """Itineraries saved before #79 repeat "Explore the area" in every spare slot."""
    free = {"activity": FREE_TIME, "cost": 0}
    days = [
        {"day": 1, "date": "2027-12-10", "morning": _slot(_FORT), "afternoon": free, "evening": free},
        {"day": 2, "date": "2027-12-11", "morning": free, "afternoon": free, "evening": free},
    ]
    plan = _plan({"days": days, "total_cost": 0})
    assert [[stop.name for stop in day.stops] for day in plan.days] == [["Fort Aguada"], [FREE_TIME]]


def test_a_place_without_coordinates_is_listed_but_not_pinned():
    days = [
        {
            "day": 1,
            "date": "2027-12-10",
            "morning": {"activity": "Hidden Cove", "cost": 0, "lat": None, "lng": None},
            "afternoon": _slot(_BEACH),
            "evening": {"activity": "Night Market", "cost": 0, "lat": 15.5},  # half a position is no position
        }
    ]
    plan = _plan({"days": days, "total_cost": 0})
    assert [stop.order for stop in plan.days[0].stops] == [None, 1, None]  # numbering skips what has no pin
    features = map_features(plan)
    assert [pin.name for pin in features.activities] == ["Baga Beach"]
    assert features.unmapped == ("Day 1 · Morning: Hidden Cove", "Day 1 · Evening: Night Market")
    assert "No map location for this place" in stop_facts(plan.days[0].stops[0])


def test_map_features_are_the_web_maps():
    features = map_features(_plan())
    assert [(pin.day, pin.order, pin.name) for pin in features.activities] == [
        (1, 1, "Fort Aguada"),
        (1, 2, "Baga Beach"),
        (2, 1, "Basilica of Bom Jesus"),
    ]
    assert [hotel.name for hotel in features.hotels] == ["Goa Grand"]  # one pin, however many nights
    assert [(airport.code, airport.role) for airport in features.airports] == [
        ("DEL", "origin"),
        ("GOI", "destination"),
    ]
    assert [day for day, _ in features.routes] == [1]  # a day with one stop has no line
    assert features.days == (1, 2) and features.unmapped == ()
    # the map frames the destination, not the journey: every pin but the departure airport
    assert len(features.framed) == 3 + 1 + 1
    assert (28.5585, 77.1002) not in features.framed


@pytest.mark.parametrize(
    ("total", "state", "text", "percent"),
    [
        (17_200, "under", "₹32,800 under budget", 34),
        (44_999, "under", "₹5,001 under budget", 90),
        (45_000, "tight", "Only ₹5,000 of the budget left", 90),  # 90% and up is tight
        (50_000, "tight", "Only ₹0 of the budget left", 100),
        (50_500, "over", "₹500 over budget", 101),
    ],
)
def test_budget_note_uses_the_cost_cards_thresholds(total, state, text, percent):
    note = budget_note(total, 50_000)
    assert (note.state, note.text, note.percent) == (state, text, percent)


def test_no_budget_no_note():
    assert budget_note(17_200, None) is None


def test_stay_is_counted_per_hotel():
    moved = {**_HOTEL, "name": "Baga Beach House", "cost_per_night": 6000.0}
    days = [{**GOA["days"][0]}, {**GOA["days"][1], "hotel": moved}, GOA["days"][2]]
    plan = _plan({"days": days, "total_cost": 18_700})
    assert [(s.name, s.nights, s.total) for s in stay_totals(plan)] == [
        ("Goa Grand", 1, 4500),
        ("Baga Beach House", 1, 6000),
    ]
    assert nights_stayed(plan) == 2
    no_hotel = _plan({"days": [{**day, "hotel": None} for day in GOA["days"]], "total_cost": 8200})
    assert nights_stayed(no_hotel) == 2  # the trip's own length


def test_row_wording():
    plan = _plan()
    outbound = plan.flight
    assert flight_route(outbound) == "DEL → GOI"
    assert flight_facts(outbound) == ["6E-204", "06:00 – 08:15", "2h 15m", "Non-stop"]
    assert stay_facts(plan.days[0].stay) == ["4-star", "8.4/10 guest rating", "Calangute"]
    assert stop_facts(plan.days[0].stops[0]) == ["History", "Top attraction", "Heritage site"]
    assert stop_facts(plan.days[0].stops[1]) == ["Beach", "Popular"]


@pytest.mark.parametrize(
    ("destination", "name"),
    [
        ("Goa", "trip-goa-2027-12-10.pdf"),
        ("New Delhi & Agra!", "trip-new-delhi-agra-2027-12-10.pdf"),
        ("São Tomé", "trip-sao-tome-2027-12-10.pdf"),
        ("गोवा", "trip-2027-12-10.pdf"),  # nothing left in ASCII: the date alone still names the file
        ('Goa"; rm -rf /', "trip-goa-rm-rf-2027-12-10.pdf"),  # nothing that could end the header's quoted string
        ("a" * 200, f"trip-{'a' * 60}-2027-12-10.pdf"),
    ],
)
def test_export_filename(destination, name):
    assert export_filename(destination, START) == name


@pytest.mark.parametrize("data", [None, {}, {"days": []}, {"days": "soon"}, {"days": [None, "x"]}, "not json", 42])
def test_nothing_to_export(data):
    with pytest.raises(NothingToExport):
        _plan(data)


def test_a_malformed_itinerary_is_read_as_far_as_it_goes():
    """Whatever the page can show, the PDF can print: odd values become "nothing", not a crash."""
    days = [
        {
            "day": "one",
            "date": None,
            "morning": {"activity": "  Fort Aguada  ", "cost": None, "lat": "15.4", "lng": True, "rating": "high"},
            "afternoon": "the beach",
            "evening": {"cost": 100},
            "hotel": {"cost_per_night": "cheap", "stars": 0, "rating": 0},
            "flight": {"price_inr": None, "stops": None, "origin": {"name": "nowhere"}, "destination": None},
        }
    ]
    plan = _plan({"days": days, "total_cost": float("nan")})
    day = plan.days[0]
    assert (day.number, day.date) == (1, "")
    assert [(stop.name, stop.cost, stop.order, stop.rating) for stop in day.stops] == [("Fort Aguada", 0.0, None, None)]
    assert (day.stay.name, day.stay.cost_per_night, day.stay.stars, day.stay.rating) == ("Hotel", 0.0, None, None)
    assert day.flight.price is None and flight_route(day.flight) == "" and flight_facts(day.flight) == []
    assert plan.costs.total == 0
    assert build_pdf(plan).startswith(b"%PDF")


# ── Static map: where things are ───────────────────────────────────────────


def test_projection_is_web_mercator():
    assert project(0, 0, 0) == (128.0, 128.0)
    # 45°N 90°E, worked by hand: x = 3/4 of the world, y = (1 − asinh(1)/π) / 2 of it
    assert project(45, 90, 2) == pytest.approx((768.0, 368.3585), abs=0.001)
    x, y = project(18.9220, 72.8347, 10)  # the Gateway of India, Mumbai
    assert (int(x // 256), int(y // 256)) == (719, 457)  # its slippy-map tile at zoom 10
    assert project(89.9, 0, 0)[1] == pytest.approx(0, abs=0.01)  # past where the map ends, it is clamped


def test_view_frames_every_point_inside_the_padding():
    points = map_features(_plan()).framed
    view = choose_view(points)
    for lat, lng in points:
        x, y = project(lat, lng, view.zoom)
        assert PADDING <= x - view.left <= WIDTH - PADDING
        assert PADDING <= y - view.top <= HEIGHT - PADDING
    # ...and one zoom level closer, they would no longer fit
    xs, ys = zip(*(project(lat, lng, view.zoom + 1) for lat, lng in points))
    assert max(xs) - min(xs) > WIDTH - 2 * PADDING or max(ys) - min(ys) > HEIGHT - 2 * PADDING


def test_view_of_one_place_and_of_the_same_place_twice():
    one = choose_view([(15.5, 73.8)])
    assert one.zoom == SINGLE_POINT_ZOOM
    x, y = project(15.5, 73.8, one.zoom)
    assert (x - one.left, y - one.top) == pytest.approx((WIDTH / 2, HEIGHT / 2))
    assert choose_view([(15.5, 73.8), (15.5, 73.8)]).zoom == MAX_ZOOM
    with pytest.raises(StaticMapError):
        choose_view([])


def test_tiles_cover_the_view():
    view = choose_view(map_features(_plan()).framed)
    grid = tile_grid(view)
    columns, rows = {c for c, _ in grid}, {r for _, r in grid}
    assert min(columns) * 256 <= view.left and (max(columns) + 1) * 256 >= view.left + WIDTH
    assert min(rows) * 256 <= view.top and (max(rows) + 1) * 256 >= view.top + HEIGHT
    assert len(grid) == len(columns) * len(rows) <= 20


def test_tile_url_fills_a_leaflet_template():
    assert tile_url("https://t.example/{z}/{x}/{y}.png", 11, 1443, 934) == "https://t.example/11/1443/934.png"
    assert tile_url("https://{s}.t.example/{z}/{x}/{y}{r}.png?key=k", 2, 1, 1) == "https://c.t.example/2/1/1.png?key=k"
    assert tile_url("{z}/{x}/{y}", 2, 5, 1) == "2/1/1"  # the world repeats sideways
    assert tile_url("{z}/{x}/{y}", 2, -1, 1) == "2/3/1"


def test_spread_separates_pins_that_would_hide_each_other():
    # three pins on one spot — a hotel beside the first two stops — end up side by side
    apart = spread([(100.0, 100.0)] * 3, 22)
    gaps = [((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 for i, (ax, ay) in enumerate(apart) for bx, by in apart[i + 1 :]]
    assert min(gaps) >= 21.9
    assert max(abs(x - 100) + abs(y - 100) for x, y in apart) < 44  # and they stay where they belong
    assert spread([(100.0, 100.0)] * 3, 22) == apart  # the same plan draws the same map
    # a pair that is merely close keeps its bearing; one that is far apart is not touched
    assert spread([(100.0, 100.0), (100.0, 110.0)], 22) == [(100.0, 94.0), (100.0, 116.0)]
    assert spread([(100.0, 100.0), (200.0, 200.0)], 22) == [(100.0, 100.0), (200.0, 200.0)]


# ── Static map: the picture ────────────────────────────────────────────────


async def _render(pins=None, routes=None, frame=None, **options) -> bytes:
    features = map_features(_plan())
    pins = (
        pins
        if pins is not None
        else [MapPin(p.lat, p.lng, day_style(p.day), str(p.order)) for p in features.activities]
    )
    return await render_static_map(
        pins, routes or [], frame or features.framed, tile_url_template=TILES, attribution="© Somebody", **options
    )


def _close(pixel: tuple[int, int, int], colour: str, tolerance: int = 40) -> bool:
    wanted = tuple(int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, wanted))


@pytest.mark.asyncio
async def test_static_map_is_a_jpeg_of_the_view_with_the_pin_where_the_place_is(tiles):
    place = (15.5, 73.8)
    picture = Image.open(io.BytesIO(await _render(pins=[MapPin(*place, day_style(2), "1")], frame=[place])))
    assert (picture.format, picture.size) == ("JPEG", (WIDTH * SCALE, HEIGHT * SCALE))
    centre = (picture.width // 2, picture.height // 2)
    beside_the_number = (centre[0] - PIN_SIZE * SCALE // 3, centre[1])
    assert _close(picture.getpixel(beside_the_number), day_style(2).color)  # day 2's green, in the middle of the view
    assert not _close(picture.getpixel((60, 60)), day_style(2).color)  # and only there


@pytest.mark.asyncio
async def test_static_map_asks_for_exactly_the_tiles_of_the_view_and_says_who_it_is(tiles):
    await _render()
    grid = tile_grid(choose_view(map_features(_plan()).framed))
    assert tiles.await_count == len(grid)
    asked = {call.args[1] for call in tiles.await_args_list}
    assert asked == {tile_url(TILES, choose_view(map_features(_plan()).framed).zoom, c, r) for c, r in grid}
    # OpenStreetMap's tile policy: an identifying User-Agent
    assert "tripplanner-ai" in tiles.await_args.args[0].headers["User-Agent"]


@pytest.mark.asyncio
async def test_routes_and_icons_are_drawn_without_error(tiles):
    features = map_features(_plan())
    pins = [MapPin(p.lat, p.lng, HOTEL_STYLE, icon="bed") for p in features.hotels]
    pins += [MapPin(p.lat, p.lng, day_style(15), "3") for p in features.activities]  # a diamond
    pins.append(MapPin(28.5585, 77.1002, day_style(1), icon="plane"))  # Delhi: far off the picture, simply not drawn
    routes = [MapRoute(day_style(day).color, points) for day, points in features.routes]
    assert (await _render(pins=pins, routes=routes)).startswith(b"\xff\xd8")


@pytest.mark.asyncio
async def test_tiles_are_cached_for_a_week_and_reused(tiles):
    cache = DictCache()
    first = await _render(cache=cache)
    downloads = tiles.await_count
    assert len(cache.data) == downloads and set(cache.ttl.values()) == {TILE_TTL} and TILE_TTL >= 7 * 24 * 3600
    assert all(key.startswith("maptile:") for key in cache.data)
    assert base64.b64decode(next(iter(cache.data.values()))) == map_tile()

    assert await _render(cache=cache) == first  # the same picture...
    assert tiles.await_count == downloads  # ...without asking the tile server again


@pytest.mark.asyncio
async def test_a_cache_that_fails_or_holds_rubbish_does_not_cost_the_map(tiles):
    broken = AsyncMock()
    broken.get.side_effect = ConnectionError("redis is away")
    broken.set.side_effect = ConnectionError("redis is away")
    assert (await _render(cache=broken)).startswith(b"\xff\xd8")

    cache = DictCache()
    await _render(cache=cache)
    cache.data = {key: "bm90IGEgdGlsZQ==" for key in cache.data}  # "not a tile"
    tiles.reset_mock()
    assert (await _render(cache=cache)).startswith(b"\xff\xd8")
    assert tiles.await_count == len(cache.data)  # every bad entry was fetched again...
    assert base64.b64decode(next(iter(cache.data.values()))) == map_tile()  # ...and replaced


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://tiles.example/11/1/1.png?key=SECRET-KEY")
    return httpx.HTTPStatusError(
        f"{status} for {request.url}", request=request, response=httpx.Response(status, request=request)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "says"),
    [
        (_http_error(503), "HTTP 503"),
        (
            httpx.ConnectError("no route to https://tiles.example/?key=SECRET-KEY"),
            "could not be reached (ConnectError)",
        ),
        (httpx.ReadTimeout("slow https://tiles.example/?key=SECRET-KEY"), "could not be reached (ReadTimeout)"),
    ],
)
async def test_a_tile_server_failure_is_a_static_map_error_that_never_leaks_the_url(tiles, failure, says):
    """A hosted tile provider takes its key in the URL; httpx puts the URL in its error messages (#60, #70)."""
    tiles.side_effect = failure
    cache = DictCache()
    with pytest.raises(StaticMapError) as raised:
        await _render(cache=cache)
    assert says in str(raised.value)
    assert "SECRET-KEY" not in str(raised.value) and "tiles.example" not in str(raised.value)
    assert raised.value.__cause__ is None  # nor in a chained traceback
    assert cache.data == {}  # nothing is kept from a map that failed


@pytest.mark.asyncio
async def test_a_dropped_connection_is_retried_once_an_http_error_is_not(tiles):
    calls = {"n": 0}

    async def flaky(client, url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("reset")
        return await fake_tile(client, url)

    tiles.side_effect = flaky
    assert (await _render()).startswith(b"\xff\xd8")
    assert tiles.await_count == len(tile_grid(choose_view(map_features(_plan()).framed))) + 1

    tiles.reset_mock()
    tiles.side_effect = _http_error(404)
    with pytest.raises(StaticMapError):
        await _render()
    assert tiles.await_count <= len(tile_grid(choose_view(map_features(_plan()).framed)))  # no second round


@pytest.mark.asyncio
async def test_a_body_that_is_not_a_picture_is_a_static_map_error_and_is_not_cached(tiles):
    """A rate-limit page answered with 200 must not sit in the cache as "the tile" for a week."""
    tiles.side_effect = None
    tiles.return_value = b"<html>Rate limited</html>"
    cache = DictCache()
    with pytest.raises(StaticMapError, match="could not be drawn"):
        await _render(cache=cache)
    assert cache.data == {}


@pytest.mark.asyncio
async def test_slow_tiles_time_out_instead_of_holding_the_export(tiles):
    async def never(_client, _url):
        await asyncio.sleep(30)

    tiles.side_effect = never
    with patch.object(static_map_module, "RENDER_TIMEOUT", 0.05), pytest.raises(StaticMapError, match="took longer"):
        await _render()


@pytest.mark.asyncio
async def test_no_tile_server_configured_is_a_static_map_error(tiles):
    with pytest.raises(StaticMapError, match="MAP_TILE_URL"):
        await render_static_map([], [], [(15.5, 73.8)], tile_url_template="")
    tiles.assert_not_awaited()


# ── Document ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def map_picture() -> bytes:
    """A stand-in for the static map: any JPEG will do for laying the page out."""
    out = io.BytesIO()
    Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), "#dfe6dc").save(out, "JPEG")
    return out.getvalue()


def test_pdf_has_the_four_sections(map_picture):
    """Roadmap acceptance: cover, day-by-day pages, cost breakdown page, static map image."""
    pdf = build_pdf(_plan(), map_picture, prepared_on=date(2026, 10, 3))
    assert pdf.startswith(b"%PDF-")
    cover, days, costs, on_the_map = _pages(pdf)

    # cover: destination, dates, total cost
    for words in ("TripPlanner", "Goa", "10 – 12 Dec 2027", "2 nights", "2 travellers", "beach", "food"):
        assert words in cover
    assert (
        "ESTIMATED TOTAL ₹17,200" in cover and "₹32,800 under budget" in cover and "34% of your ₹50,000 budget" in cover
    )
    assert "DEL → GOI · 6E-204 · 06:00 – 08:15 · 2h 15m · Non-stop" in cover
    assert "Goa Grand · 2 nights" in cover and "3 days · 3 places to see" in cover
    assert "Prepared on 3 Oct 2026" in cover and "Nothing in this plan has been booked" in cover

    # day by day: morning / afternoon / evening + hotel + day cost
    assert "Day by day" in days
    for words in (
        "DAY 1 Fri, 10 Dec ₹4,500 stay + activities",
        "FLIGHT DEL → GOI 6E-204 · 06:00 – 08:15 · 2h 15m · Non-stop ₹8,200 return",
        "MORNING Fort Aguada History · Top attraction · Heritage site",
        "AFTERNOON Baga Beach Beach · Popular",
        "STAY Goa Grand 4-star · 8.4/10 guest rating · Calangute ₹4,500 per night",
        "DAY 2 Sat, 11 Dec",
        "Basilica of Bom Jesus Spiritual site",
        "DAY 3 Sun, 12 Dec ALL DAY Free time Nothing booked",
    ):
        assert words in days, words
    assert days.count("Free time") == 1

    # cost breakdown: flights + hotels + activities
    assert "Cost breakdown" in costs
    for words in (
        "Flights DEL → GOI · return · 2 travellers ₹8,200",
        "Stay Goa Grand · 2 nights × ₹4,500 ₹9,000",
        "Activities No entry fees listed ₹0",
        "Estimated total ₹17,200",
        "Your budget ₹50,000",
        "Under budget ₹32,800",
        "Per traveller ₹17,200 ÷ 2 ₹8,600",
        "1 Fri, 10 Dec ₹4,500 ₹0 ₹4,500",
        "Total ₹9,000 ₹0 ₹17,200",
    ):
        assert words in costs, words

    # the map, with what each pin is
    assert "On the map" in on_the_map and "3 stops, numbered in visiting order" in on_the_map
    for words in ("Day 1", "Day 2", "Hotel", "Airport", "Fort Aguada — Day 1, morning", "Goa Grand — where you stay"):
        assert words in on_the_map, words
    assert "GOI · Goa Airport — where you land" in on_the_map and "DEL" not in on_the_map
    assert _images(pdf) == [0, 0, 0, 1]

    assert _page_numbers(_pages(pdf)) == ["2 of 4", "3 of 4", "4 of 4"] and "Page" not in cover
    # the running head of every page after the cover
    assert all(page.startswith("Goa · 10 – 12 Dec 2027 TripPlanner") for page in (days, costs, on_the_map))


def test_pdf_without_a_map_has_three_sections_and_does_not_mention_one():
    pdf = build_pdf(_plan(), None)
    pages = _pages(pdf)
    assert len(pages) == 3 and _images(pdf) == [0, 0, 0]
    assert "On the map" not in " ".join(pages) and "the same one on the map" not in pages[1]
    assert _page_numbers(pages) == ["2 of 3", "3 of 3"]


def test_pdf_describes_itself(map_picture):
    reader = PdfReader(io.BytesIO(build_pdf(_plan(), map_picture)))
    assert reader.metadata.title == "Goa — 10 – 12 Dec 2027" and reader.metadata.author == "TripPlanner AI"
    assert [(entry.title, reader.get_destination_page_number(entry) + 1) for entry in reader.outline] == [
        ("Day by day", 2),
        ("Cost breakdown", 3),
        ("On the map", 4),
    ]
    assert reader.trailer["/Root"]["/Lang"] == "en"
    # A4, with the site's two typefaces embedded — nothing is left to whatever the reader's machine has
    assert [round(float(v)) for v in reader.pages[0].mediabox[2:]] == [595, 842]
    fonts = {
        str(font["/BaseFont"]).split("+")[-1]: "/FontFile2" in font.get("/FontDescriptor", {})
        for page in reader.pages
        for font in (ref.get_object() for ref in page["/Resources"]["/Font"].values())
    }
    assert {name for name, embedded in fonts.items() if embedded} == {
        "Inter-Regular",
        "Inter-Medium",
        "Inter-SemiBold",
        "Fraunces-Medium",
    }
    for page in reader.pages:  # no transparency: some printers still mishandle it
        assert "/ExtGState" not in page["/Resources"]
        assert "/F1 " not in page.get_contents().get_data().decode("latin-1")  # Helvetica is never used


def test_text_from_a_plan_is_never_markup():
    """ReportLab paragraphs read tags; a hotel called "Tom & Jerry's <b>Inn</b>" must print as written."""
    spiky = "Tom & Jerry's <b>Inn</b> </para>"
    days = [
        {
            **GOA["days"][0],
            "morning": {**_slot(_FORT), "activity": "Fort <i>Aguada</i> & Co"},
            "hotel": {**_HOTEL, "name": spiky, "address": "<img src=x>"},
        }
    ]
    plan = _plan({"days": days, "total_cost": 12_700}, destination="R&D <Trip>", interests=["<food>"])
    everything = " ".join(_pages(build_pdf(plan, None)))
    for words in ("R&D <Trip>", spiky, "<img src=x>", "Fort <i>Aguada</i> & Co", "<food>"):
        assert words in everything, words


def test_a_destination_the_fonts_cannot_draw_still_exports():
    """Devanagari is outside the bundled fonts: it prints as boxes, but the PDF is built and named."""
    plan = _plan(destination="गोवा")
    pages = _pages(build_pdf(plan, None))
    assert len(pages) == 3 and "₹17,200" in pages[0]
    assert pages[1].startswith("10 – 12 Dec 2027")  # the running head drops what it cannot draw
    assert export_filename(plan.destination, plan.start_date) == "trip-2027-12-10.pdf"


def test_a_long_trip_runs_over_several_pages_and_every_day_is_there(map_picture):
    days = [
        {
            "day": n,
            "date": str(START + timedelta(days=n - 1)),
            "morning": {**_slot(_FORT), "activity": f"Place {n}"},
            "afternoon": {**_slot(_BEACH), "activity": f"Second place {n}"},
            "evening": {**_slot(_BASILICA), "activity": f"Third place {n}"},
            "hotel": _HOTEL if n < 15 else None,
            "flight": flight(8200.0) if n == 1 else None,
        }
        for n in range(1, 16)
    ]
    plan = _plan({"days": days, "total_cost": 8200 + 14 * 4500}, end_date=START + timedelta(days=14))
    pdf = build_pdf(plan, map_picture)
    pages = _pages(pdf)
    text = " ".join(pages)
    assert len(pages) > 6
    assert all(f"DAY {n} " in text and f"Third place {n}" in text for n in range(1, 16))
    assert _page_numbers(pages) == [f"{number} of {len(pages)}" for number in range(2, len(pages) + 1)]
    assert "14 nights × ₹4,500" in text and "45 stops" in pages[-2] + pages[-1]
    assert sum(_images(pdf)) == 1


def test_absurdly_long_names_are_cut_instead_of_breaking_the_layout(map_picture):
    """A card cannot run on to the next page, so one endless name used to make the whole PDF impossible to build."""
    endless = "Very long place name " * 300
    slot = {**_slot(_FORT), "activity": endless}
    days = [
        {
            **GOA["days"][0],
            "morning": slot,
            "afternoon": {**slot, "activity": endless + "2"},
            "evening": {**slot, "activity": endless + "3"},
            "hotel": {**_HOTEL, "name": endless, "address": endless},
        }
    ]
    plan = _plan(
        {"days": days, "total_cost": 12_700},
        destination="An endless destination " * 9,  # 207 characters; the trip table allows 200
        interests=[f"interest number {n} " * 5 for n in range(40)],
    )
    pdf = build_pdf(plan, map_picture)
    pages = _pages(pdf)
    assert len(pages) == 4
    assert "Very long place name Very long place name" in pages[1] and "…" in pages[1]
    assert max(len(page) for page in pages) < 5_000  # nothing was printed in full
    assert pages[0].count("interest number") == 12 * 2  # twelve chips, each cut to forty characters


def test_a_day_card_is_never_split_across_pages():
    assert Card(MagicMock(), 100).split(100, 10) == []


@pytest.mark.parametrize(
    ("total", "on_the_cover", "in_the_ledger"),
    [
        (47_000, "Only ₹3,000 of the budget left", "Left of the budget ₹3,000"),
        (56_500, "₹6,500 over budget", "Over budget ₹6,500"),
    ],
)
def test_a_tight_or_blown_budget_is_said_in_words(total, on_the_cover, in_the_ledger):
    cover, _, costs = _pages(build_pdf(_plan({**GOA, "total_cost": total}), None))
    assert on_the_cover in cover and in_the_ledger in costs
    assert f"Other Part of the plan's total {format_inr(total - 17_200)}" in costs  # the ledger still adds up


def test_no_flight_and_no_hotel_are_said_not_left_blank():
    days = [{**day, "hotel": None, "flight": None} for day in GOA["days"]]
    cover, _, costs = _pages(build_pdf(_plan({"days": days, "total_cost": 0}, budget=None), None))
    assert cover.count("Not included in this plan") == 2 and "budget" not in cover
    assert "Flights Not included ₹0" in costs and "Stay Not included ₹0" in costs
    assert "Your budget" not in costs
    assert "Per traveller" not in costs  # two travellers, but nothing to share out
    assert "Flights" not in costs.split("DAY TOTAL")[1]  # nor a flights line among the days


def test_pdfs_can_be_built_from_several_threads_at_once(map_picture):
    """Every document shares the registered fonts, and ReportLab's subsetting of them is not re-entrant.

    build_pdf takes a lock; without it, concurrent exports fail inside makeSubset
    (IndexError / KeyError). Threads are switched as often as possible here so
    that a missing lock shows up on every run, not once in a while.
    """
    plans = [_plan(destination=f"Place {n}") for n in range(16)]
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            pdfs = list(pool.map(lambda plan: build_pdf(plan, map_picture), plans))
    finally:
        sys.setswitchinterval(interval)
    for n, pdf in enumerate(pdfs):
        pages = _pages(pdf)
        assert len(pages) == 4 and f"Place {n}" in pages[0] and f"Place {n} ·" in pages[1]


@pytest.mark.asyncio
async def test_what_the_builder_saves_is_what_the_pdf_prints():
    """The contract between Phase 12's builder and the export: its real output, straight into the PDF."""
    meta = {"destination": "Goa", "start_date": "2027-12-10", "end_date": "2027-12-12", "group_size": 2}
    with patch("src.ai.builder.builder._call_llm", fake_builder_llm):
        draft = await build_itinerary(meta, [flight(8200.0, day="2027-12-10")], HOTELS, ATTRACTIONS)
    plan = _plan(draft.model_dump())
    assert plan.costs == _plan().costs
    assert [[stop.name for stop in day.stops] for day in plan.days] == [
        ["Fort Aguada", "Baga Beach"],
        ["Basilica of Bom Jesus"],
        [FREE_TIME],
    ]
    assert len(map_features(plan).activities) == 3 and map_features(plan).unmapped == ()
    assert "Fort Aguada" in _pages(build_pdf(plan, None))[1]


# ── Export: the map is the only part allowed to be missing ─────────────────


@pytest.mark.asyncio
async def test_export_includes_the_map(tiles):
    exported = await export_itinerary(_plan(), cache=DictCache(), trip_id="t-1")
    assert (exported.filename, exported.map_status) == ("trip-goa-2027-12-10.pdf", MAP_INCLUDED)
    assert _images(exported.content) == [0, 0, 0, 1]


@pytest.mark.asyncio
async def test_export_survives_a_tile_server_that_is_down(tiles, caplog):
    """Roadmap acceptance: static map failure → omit the image, log a warning, the PDF still downloads."""
    tiles.side_effect = _http_error(503)
    with caplog.at_level(logging.WARNING, logger="app.pdf.export"):
        exported = await export_itinerary(_plan(), trip_id="trip-42")

    assert exported.map_status == MAP_UNAVAILABLE
    assert len(_pages(exported.content)) == 3 and sum(_images(exported.content)) == 0
    assert "Cost breakdown" in _pages(exported.content)[2]
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings == ["PDF export for trip trip-42: the map was left out — the tile server answered HTTP 503"]


@pytest.mark.asyncio
async def test_export_without_any_location_does_not_ask_for_tiles(tiles, caplog):
    days = [{"day": 1, "date": "2027-12-10", "morning": {"activity": "Hidden Cove", "cost": 0}, "hotel": None}]
    with caplog.at_level(logging.WARNING, logger="app.pdf.export"):
        exported = await export_itinerary(_plan({"days": days, "total_cost": 0}))
    assert exported.map_status == MAP_NONE and len(_pages(exported.content)) == 3
    tiles.assert_not_awaited()
    assert caplog.records == []  # nothing went wrong: there was simply nothing to map


@pytest.mark.asyncio
async def test_export_with_no_tile_server_configured_has_no_map(tiles):
    with patch("app.pdf.export.settings") as settings:
        settings.MAP_TILE_URL = ""
        exported = await export_itinerary(_plan())
    assert exported.map_status == MAP_NONE
    tiles.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_pdf_is_laid_out_off_the_event_loop(tiles):
    """Laying out pages is CPU work; on the loop it would stall every open SSE stream."""
    built_on = []

    def record(*_args, **_kwargs) -> bytes:
        built_on.append(threading.current_thread())
        return b"%PDF-stub"

    with patch("app.pdf.export.build_pdf", record):
        await export_itinerary(_plan())
    assert built_on and built_on[0] is not threading.main_thread()


# ── Route ──────────────────────────────────────────────────────────────────


def _trip(owner: uuid.UUID, **fields) -> Trip:
    defaults = {
        "id": uuid.uuid4(),
        "user_id": owner,
        "destination": "Goa",
        "start_date": START,
        "end_date": START + timedelta(days=2),
        "budget": 50_000.0,
        "group_size": 2,
        "interests": ["beach", "food"],
        "status": "completed",
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }
    return Trip(**{**defaults, **fields})


def _itinerary(trip: Trip, data: object = GOA) -> Itinerary:
    return Itinerary(trip_id=trip.id, structured_data=data, total_cost=17_200.0)


@contextmanager
def _client(trip: Trip | None, itinerary: Itinerary | None, *, authenticated: bool = True, cache=None):
    """App client with auth, the database and the tile cache overridden."""
    from app.api.deps import get_current_user, get_db, get_redis_or_none
    from app.main import app

    db = AsyncMock()
    trip_result, itinerary_result = MagicMock(), MagicMock()
    trip_result.scalar_one_or_none.return_value = trip
    itinerary_result.scalar_one_or_none.return_value = itinerary
    db.execute = AsyncMock(side_effect=[trip_result, itinerary_result])

    async def _db():
        yield db

    overrides = {get_db: _db, get_redis_or_none: lambda: cache}
    if authenticated:
        overrides[get_current_user] = lambda: MagicMock(id=trip.user_id if trip else uuid.uuid4())
    app.dependency_overrides.update(overrides)
    try:
        yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), db
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_export_route_sends_the_pdf_as_a_download(tiles):
    """Roadmap acceptance: Content-Type application/pdf, Content-Disposition attachment; filename=trip-{destination}-{date}.pdf."""
    trip = _trip(uuid.uuid4())
    cache = DictCache()
    with _client(trip, _itinerary(trip), cache=cache) as (client, db):
        response = await client.get(f"/trips/{trip.id}/export/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="trip-goa-2027-12-10.pdf"'
    assert response.headers["x-itinerary-map"] == "included"
    assert response.headers["cache-control"] == "private, no-store"
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.content.startswith(b"%PDF-") and response.content.rstrip().endswith(b"%%EOF")

    pages = _pages(response.content)
    assert len(pages) == 4 and "Goa" in pages[0] and "Fort Aguada" in pages[1] and "₹17,200" in pages[2]
    assert _images(response.content) == [0, 0, 0, 1]
    assert cache.data  # the tiles went into the cache the route was given
    db.close.assert_awaited()  # the DB connection is released before the map is fetched


@pytest.mark.asyncio
async def test_export_route_still_answers_when_the_map_cannot_be_drawn(caplog):
    """No tile stub here: the autouse fixture makes every download fail, like a tile server that is down."""
    trip = _trip(uuid.uuid4())
    with caplog.at_level(logging.WARNING, logger="app.pdf.export"), _client(trip, _itinerary(trip)) as (client, _):
        response = await client.get(f"/trips/{trip.id}/export/pdf")

    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
    assert response.headers["x-itinerary-map"] == "unavailable"
    assert len(_pages(response.content)) == 3
    assert any(
        str(trip.id) in record.getMessage() and "map was left out" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_export_route_requires_a_token():
    trip = _trip(uuid.uuid4())
    with _client(trip, _itinerary(trip), authenticated=False) as (client, _):
        response = await client.get(f"/trips/{trip.id}/export/pdf")
    assert response.status_code == 401 and response.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_export_route_404s_for_a_trip_that_is_not_yours():
    """The lookup is by trip id AND user id, so someone else's trip is simply not found."""
    with _client(None, None) as (client, _):
        response = await client.get(f"/trips/{uuid.uuid4()}/export/pdf")
    assert response.status_code == 404 and response.json()["error"]["message"] == "Trip not found."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("missing", "No itinerary has been generated for this trip yet."),
        (None, "This itinerary has no day-by-day plan to export."),
        ({"days": []}, "This itinerary has no day-by-day plan to export."),
    ],
)
async def test_export_route_404s_when_there_is_nothing_to_export(tiles, data, message):
    trip = _trip(uuid.uuid4(), status="pending")
    with _client(trip, None if data == "missing" else _itinerary(trip, data)) as (client, _):
        response = await client.get(f"/trips/{trip.id}/export/pdf")
    assert response.status_code == 404
    assert response.json()["error"] == {"code": "NOT_FOUND", "message": message}
    tiles.assert_not_awaited()


@pytest.mark.asyncio
async def test_export_route_reports_a_failed_build_in_the_error_envelope(tiles):
    trip = _trip(uuid.uuid4())
    with patch("app.pdf.export.build_pdf", side_effect=RuntimeError("layout exploded")):
        with _client(trip, _itinerary(trip)) as (client, _):
            response = await client.get(f"/trips/{trip.id}/export/pdf")
    assert response.status_code == 500
    assert response.json()["error"] == {"code": "INTERNAL_SERVER_ERROR", "message": "The PDF could not be generated."}


@pytest.mark.asyncio
async def test_export_headers_are_readable_from_the_frontends_origin(tiles):
    """A page on another origin cannot read Content-Disposition unless CORS exposes it."""
    trip = _trip(uuid.uuid4())
    with _client(trip, _itinerary(trip)) as (client, _):
        response = await client.get(f"/trips/{trip.id}/export/pdf", headers={"Origin": "http://localhost:3000"})
    exposed = {name.strip().lower() for name in response.headers["access-control-expose-headers"].split(",")}
    assert {"content-disposition", "x-itinerary-map"} <= exposed


@pytest.mark.asyncio
async def test_the_tile_cache_is_optional():
    """Redis is only a cache here: without it the export still works, it just fetches every tile."""
    from app.api.deps import get_redis_or_none

    with patch("app.db.redis.redis_client", None):
        assert await get_redis_or_none() is None
    client = AsyncMock()
    with patch("app.db.redis.redis_client", client):
        assert await get_redis_or_none() is client

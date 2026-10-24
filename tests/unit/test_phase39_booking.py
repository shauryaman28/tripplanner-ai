"""Phase 39 — where a traveller books the plan's flight and hotel.

The roadmap's acceptance, and where each is held:

  Dev A  - each flight result has a Google Flights link pre-filling the route and date   → the tool's tests here
         - each hotel result has a Booking.com link                                       → the tool's tests here
         - the links open the right search in a browser                                   → opened in Chromium before
                                                                                             they were written
                                                                                             (docs/phase39_build_log.md)
  Dev B  - the buttons, a new tab, greyed out with no link                                → e2e/booking.spec.ts
"""

import io
import json
import re
from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from pypdf import PdfReader

from app.pdf.document import build_pdf
from app.pdf.plan import build_plan
from src.ai import booking
from src.ai.builder import builder
from src.ai.mcp_server.models import FlightSearchInput, HotelSearchInput
from src.ai.mcp_server.tools import search_flights, search_hotels
from tests import fakes
from tests.fakes import ATTRACTIONS, HOTELS, FakeProviders, flight, memory_cache, providers_faked


def asked(url: str) -> tuple[str, dict]:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}", {k: v[0] for k, v in parse_qs(parts.query).items()}


# ── The links ──────────────────────────────────────────────────────────────


def test_a_flight_is_asked_of_google_flights_in_its_own_words():
    there, params = asked(booking.flight_link("del", "GOI", "2026-11-20", "2026-11-23", 2))
    assert there == "https://www.google.com/travel/flights"
    assert params == {
        "q": "Flights to GOI from DEL on 2026-11-20 through 2026-11-23 for 2 adults",
        "curr": "INR",
        "hl": "en",
    }
    _, one_way = asked(booking.flight_link("DEL", "GOI", "2026-11-20", None, 1))
    assert one_way["q"] == "One way flights to GOI from DEL on 2026-11-20"  # one traveller: Google's default, unsaid


def test_a_hotel_is_asked_of_booking_com_by_its_name_and_its_town():
    there, params = asked(booking.hotel_link("Taj Fort Aguada Resort & Spa", "Goa", "2026-11-20", "2026-11-23", 3))
    assert there == "https://www.booking.com/searchresults.html"
    assert params == {
        "ss": "Taj Fort Aguada Resort & Spa, Goa",
        "checkin": "2026-11-20",
        "checkout": "2026-11-23",
        "group_adults": "3",
        "no_rooms": "2",  # as the hotel search asked: two adults to a room
        "group_children": "0",
        "selected_currency": "INR",
    }
    assert "%26" in booking.hotel_link("Resort & Spa", "Goa", "2026-11-20", "2026-11-23", 1)  # the & is the name's
    assert asked(booking.hotel_link(None, "Goa", "2026-11-20", "2026-11-21", 1))[1]["ss"] == "Goa"


@pytest.mark.parametrize(
    "made",
    [
        lambda: booking.flight_link("Delhi", "GOI", "2026-11-20"),  # not a code
        lambda: booking.flight_link("DEL", "GOI", "20 Nov"),  # not a date
        lambda: booking.flight_link("DEL", "GOI", "2026-11-20", "soon"),
        lambda: booking.flight_link("DEL", "GOI", "2026-11-20", travellers=10),
        lambda: booking.flight_link("DEL", "GOI", "2026-11-20", travellers=True),
        lambda: booking.hotel_link("Goa Grand", "", "2026-11-20", "2026-11-23"),
        lambda: booking.hotel_link("Goa Grand", "Goa", "2026-11-23", "2026-11-20"),  # out before in
        lambda: booking.hotel_link("Goa Grand", "Goa", "2026-11-20", "2026-11-20"),
        lambda: booking.hotel_link("Goa Grand", "Goa", "2026-11-20", "2026-11-23", 0),
    ],
)
def test_a_link_that_cannot_be_made_is_none(made):
    assert made() is None


def test_text_cannot_leave_its_parameter():
    link = booking.hotel_link('Inn"&ss=x#<script>', "Goa", "2026-11-20", "2026-11-21", 1)
    assert asked(link)[1]["ss"] == 'Inn"&ss=x#<script>, Goa' and "#" not in link and "<" not in link


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        (booking.flight_link("DEL", "GOI", "2026-11-20"), True),
        (booking.hotel_link("Goa Grand", "Goa", "2026-11-20", "2026-11-21"), True),
        ("javascript:alert(1)", False),
        ("http://www.booking.com/searchresults.html?ss=Goa", False),
        ("https://www.google.com/travel/flights.example.com/?q=x", False),
        ("https://evil.example/?https://www.booking.com/searchresults.html?", False),
        (None, False),
    ],
)
def test_only_the_two_searches_count_as_booking_links(url, ok):
    assert booking.is_booking_url(url) is ok


def test_the_page_opens_what_the_planner_makes_and_nothing_else():
    source = open("src/frontend/src/lib/booking.ts", encoding="utf-8").read()
    sites = re.findall(r'"(https://[^"]+)"', source[source.index("BOOKING_SITES") :])
    assert tuple(sites[:2]) == booking.BOOKING_SITES


# ── Each result the tools find ─────────────────────────────────────────────


def test_every_flight_the_search_finds_has_its_link_with_its_route_days_and_travellers():
    with providers_faked(FakeProviders()), memory_cache():
        found = search_flights(
            FlightSearchInput(
                origin="DEL",
                destination="Goa",
                date="2030-01-10",
                return_date="2030-01-13",
                budget=90_000,
                passengers=2,
            )
        )
        again = search_flights(  # from the cache: the link is made after it, so this has one too
            FlightSearchInput(
                origin="DEL",
                destination="Goa",
                date="2030-01-10",
                return_date="2030-01-13",
                budget=90_000,
                passengers=2,
            )
        )
    assert (
        found and all(f.booking_url for f in found) and [f.booking_url for f in again] == [f.booking_url for f in found]
    )
    assert (
        asked(found[0].booking_url)[1]["q"] == "Flights to GOI from DEL on 2030-01-10 through 2030-01-13 for 2 adults"
    )


def test_every_hotel_the_search_finds_has_its_link_with_its_nights_and_guests():
    with providers_faked(FakeProviders()), memory_cache():
        found = search_hotels(
            HotelSearchInput(
                destination="Goa", check_in="2030-01-10", check_out="2030-01-13", budget_per_night=30_000, guests=2
            )
        )
    assert found and all(h.booking_url for h in found)
    _, params = asked(found[0].booking_url)
    assert params["ss"] == f"{found[0].name}, Goa" and (params["checkin"], params["checkout"]) == (
        "2030-01-10",
        "2030-01-13",
    )
    assert params["group_adults"] == "2" and params["no_rooms"] == "1"


# ── The plan keeps them ────────────────────────────────────────────────────

TRIP_META = {"destination": "Goa", "start_date": "2030-01-10", "end_date": "2030-01-12", "group_size": 1}
LINKED_FLIGHT = flight(8_200.0) | {"booking_url": booking.flight_link("DEL", "GOI", "2030-01-10", "2030-01-12")}
LINKED_HOTELS = [h | {"booking_url": booking.hotel_link(h["name"], "Goa", "2030-01-10", "2030-01-12")} for h in HOTELS]


async def test_the_plans_flight_and_hotel_carry_the_searchs_links_and_never_the_models():
    async def links_its_own(system, user_prompt, on_token=None):
        plan = json.loads(await fakes.fake_builder_llm(system, user_prompt))
        plan["days"][0]["hotel"]["booking_url"] = "https://evil.example/book"
        return json.dumps(plan)

    with patch.object(builder, "_call_llm", links_its_own):
        result = await builder.ItineraryBuilder().run(
            trip_meta=TRIP_META, flights=[LINKED_FLIGHT], hotels=LINKED_HOTELS, attractions=ATTRACTIONS
        )
    days = result["draft"]["days"]
    assert days[0]["flight"]["booking_url"] == LINKED_FLIGHT["booking_url"]
    assert days[0]["hotel"]["booking_url"] == LINKED_HOTELS[0]["booking_url"]  # the search's, not the model's
    with patch.object(builder, "_call_llm", fakes.fake_builder_llm):
        plain = await builder.ItineraryBuilder().run(
            trip_meta=TRIP_META, flights=[flight(8_200.0)], hotels=HOTELS, attractions=ATTRACTIONS
        )
    assert "booking_url" not in plain["draft"]["days"][0]["hotel"]  # no link: no key at all


# ── The PDF ────────────────────────────────────────────────────────────────


def _links_in(pdf: bytes) -> list[str]:
    return [
        str(annotation.get_object()["/A"]["/URI"])
        for page in PdfReader(io.BytesIO(pdf)).pages
        for annotation in page.get("/Annots") or []
        if annotation.get_object().get("/A", {}).get("/URI")
    ]


def _pdf(days: list[dict]) -> bytes:
    plan = build_plan(destination="Goa", start_date=date(2030, 1, 10), end_date=date(2030, 1, 12), travellers=1,
                      budget=None, interests=None, structured_data={"total_cost": 0, "days": days})  # fmt: skip
    return build_pdf(plan, prepared_on=date(2026, 10, 18))


def test_the_pdf_links_the_flight_and_the_stay_on_its_first_night():
    hotel = {"name": "Goa Grand", "cost_per_night": 4_500, "booking_url": LINKED_HOTELS[0]["booking_url"]}
    days = [
        {
            "day": 1,
            "date": "2030-01-10",
            "flight": LINKED_FLIGHT,
            "hotel": hotel,
            "morning": {"activity": "Fort Aguada"},
        },
        {"day": 2, "date": "2030-01-11", "hotel": hotel, "morning": {"activity": "Baga Beach"}},
    ]
    assert _links_in(_pdf(days)) == [LINKED_FLIGHT["booking_url"], hotel["booking_url"]]  # once each


def test_the_pdf_links_nothing_else():
    days = [{"day": 1, "date": "2030-01-10", "flight": flight(8_200.0) | {"booking_url": "javascript:alert(1)"},
             "hotel": {"name": "Goa Grand", "cost_per_night": 4_500}, "morning": {"activity": "Fort Aguada"}}]  # fmt: skip
    assert _links_in(_pdf(days)) == []

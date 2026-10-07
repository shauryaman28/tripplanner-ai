"""Phase 18 — Map view, backend half.

Every activity the map draws needs coordinates, so they are attached to the
itinerary from the search results (never trusted from the LLM), flights carry
their airports, and the save path reports any activity it cannot pin.
"""

import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.builder.builder import ItineraryDraft, _attach_source_data, _normalise_free_time, build_itinerary
from src.ai.itinerary import FREE_TIME
from src.ai.mcp_server.models import AttractionInput, FlightSearchInput
from src.ai.mcp_server.tools import _otm_kind_to_category, get_attractions, search_flights
from src.ai.orchestrator.orchestrator import _unmapped_activities, persist_node
from tests.fakes import duffel_offer, duffel_response

ATTRACTIONS = [
    {"name": "Fort Aguada", "category": "history", "rating": 7.0, "lat": 15.492, "lng": 73.773},
    {"name": "Baga Beach", "category": "beach", "rating": 2.0, "lat": 15.556, "lng": 73.752},
]
HOTELS = [
    {
        "name": "Goa Grand",
        "stars": 4,
        "rating": 8.4,
        "address": "Calangute",
        "price_per_night_inr": 4500.0,
        "lat": 15.544,
        "lng": 73.755,
    }
]
FLIGHTS = [
    {"airline": "AI", "flight_number": "AI-1", "price_inr": 9000.0},
    {
        "airline": "6E",
        "flight_number": "6E-204",
        "price_inr": 8200.0,
        "origin": {"code": "DEL", "lat": 28.56, "lng": 77.1},
        "destination": {"code": "GOI", "lat": 15.38, "lng": 73.83},
    },
]


def _draft() -> dict:
    return {
        "days": [
            {
                "day": 1,
                "date": "2026-12-10",
                "morning": {"activity": "Fort Aguada", "cost": 0, "lat": 99.0, "lng": 99.0},  # the model got it wrong
                "afternoon": {"activity": "Baga Beach", "cost": 0},  # …or left it out
                "evening": {"activity": "Explore the area", "cost": 0, "lat": 1.0, "lng": 1.0},  # …or invented one
                "hotel": {"name": "Goa Grand", "cost_per_night": 4500.0},
                "flight": None,
            },
            {
                "day": 2,
                "date": "2026-12-11",
                "morning": None,
                "afternoon": None,
                "evening": None,
                "hotel": None,
                "flight": None,
            },
        ],
        "total_cost": 12_700.0,
        "currency": "INR",
    }


# ── Dev A: coordinates come from the source data ───────────────────────────


def test_slots_get_coordinates_category_and_rating_from_the_attractions():
    draft = _draft()
    _attach_source_data(draft, FLIGHTS, HOTELS, ATTRACTIONS)
    day = draft["days"][0]

    assert (day["morning"]["lat"], day["morning"]["lng"]) == (15.492, 73.773)  # the wrong pin is corrected
    assert (day["afternoon"]["lat"], day["afternoon"]["lng"]) == (15.556, 73.752)
    assert day["afternoon"]["category"] == "beach" and day["afternoon"]["rating"] == 2.0
    assert day["evening"]["lat"] is None and day["evening"]["category"] is None  # free time is not a place


def test_hotel_and_outbound_flight_are_attached():
    draft = _draft()
    _attach_source_data(draft, FLIGHTS, HOTELS, ATTRACTIONS)

    hotel = draft["days"][0]["hotel"]
    assert (hotel["lat"], hotel["lng"], hotel["stars"], hotel["address"]) == (15.544, 73.755, 4, "Calangute")

    flight = draft["days"][0]["flight"]
    assert flight["flight_number"] == "6E-204"  # the cheapest — the one the budget math assumes
    assert flight["origin"]["code"] == "DEL" and flight["destination"]["lat"] == 15.38
    assert draft["days"][1]["flight"] is None


def test_a_flight_the_model_put_on_another_day_is_dropped():
    """The model is not asked to pick flights; only the source flight on day 1 is shown."""
    draft = _draft()
    draft["days"][1]["flight"] = {"flight_number": "XX-999", "price_inr": 1.0}
    _attach_source_data(draft, FLIGHTS, HOTELS, ATTRACTIONS)
    assert draft["days"][1]["flight"] is None

    no_flights = _draft()
    _attach_source_data(no_flights, [], HOTELS, ATTRACTIONS)
    assert no_flights["days"][0]["flight"] is None


@pytest.mark.asyncio
async def test_built_itinerary_keeps_the_map_fields():
    """The draft model must not drop what the map needs."""
    with patch("src.ai.builder.builder._call_llm", AsyncMock(return_value=json.dumps(_draft()))):
        result = await build_itinerary({"destination": "Goa"}, FLIGHTS, HOTELS, ATTRACTIONS)

    assert isinstance(result, ItineraryDraft)
    day = result.model_dump()["days"][0]
    assert day["morning"]["lat"] == 15.492 and day["morning"]["category"] == "history"
    assert day["hotel"]["lat"] == 15.544 and day["flight"]["origin"]["code"] == "DEL"


# ── Free time is said once per day ─────────────────────────────────────────


def _slot(name: str) -> dict:
    return {"activity": name, "cost": 0.0, "lat": None, "lng": None, "category": None, "rating": None}


def test_free_time_is_dropped_from_a_day_that_has_real_activities():
    day = {"day": 1, "morning": _slot("Fort Aguada"), "afternoon": _slot(FREE_TIME), "evening": _slot(FREE_TIME)}
    _normalise_free_time({"days": [day]})
    assert day["morning"]["activity"] == "Fort Aguada"
    assert day["afternoon"] is None and day["evening"] is None


@pytest.mark.parametrize(
    "slots",
    [
        {"morning": _slot(FREE_TIME), "afternoon": _slot(FREE_TIME), "evening": _slot(FREE_TIME)},  # the live output
        {"morning": None, "afternoon": None, "evening": _slot(FREE_TIME)},
        {"morning": None, "afternoon": None, "evening": None},  # an empty day still says something
    ],
)
def test_a_day_with_no_real_activity_has_exactly_one_free_time_slot(slots):
    day = {"day": 5, **slots}
    _normalise_free_time({"days": [day]})
    assert day["morning"]["activity"] == FREE_TIME and day["morning"]["cost"] == 0.0
    assert day["afternoon"] is None and day["evening"] is None


@pytest.mark.asyncio
async def test_built_itinerary_says_free_time_once():
    draft = _draft()
    draft["days"][1].update(morning=_slot(FREE_TIME), afternoon=_slot(FREE_TIME), evening=_slot(FREE_TIME))
    with patch("src.ai.builder.builder._call_llm", AsyncMock(return_value=json.dumps(draft))):
        result = await build_itinerary({"destination": "Goa"}, FLIGHTS, HOTELS, ATTRACTIONS)

    days = result.model_dump()["days"]
    assert days[0]["evening"] is None  # day 1 has two real stops — no filler
    assert [days[1][s] and days[1][s]["activity"] for s in ("morning", "afternoon", "evening")] == [
        FREE_TIME,
        None,
        None,
    ]


# ── Categories and ratings shown in the popup come out of real OpenTripMap data ──


@pytest.mark.parametrize(
    "kinds,category",
    [
        ("natural,interesting_places,beaches,other_beaches", "beach"),
        # tagged "museums" as well — the reserve must win
        (
            "cultural,museums,gardens_and_parks,urban_environment,interesting_places,natural,nature_reserves,zoos",
            "nature",
        ),
        ("urban_environment,gardens_and_parks,cultural,museums,interesting_places,zoos", "nature"),
        ("water,natural,interesting_places,waterfalls", "nature"),
        ("gardens_and_parks,cultural,urban_environment,interesting_places", "nature"),
        ("fortifications,historic,interesting_places,other_fortifications", "history"),
        ("religion,fortifications,historic,hindu_temples,interesting_places", "history"),  # a fort with a temple
        ("view_points,other,palaces,architecture,historic_architecture,interesting_places", "history"),
        ("religion,hindu_temples,interesting_places", "spiritual"),
        ("religion,churches,interesting_places,other_churches", "spiritual"),
        ("cultural,museums,interesting_places,other_museums", "museum"),
        ("cultural,museums,interesting_places,other_museums,foods,restaurants,tourist_facilities", "museum"),
        ("accomodations,other_hotels,restaurants,foods,tourist_facilities", "food"),
        ("cinemas,cultural,theatres_and_entertainments,interesting_places", "culture"),
        ("shops,malls,tourist_facilities", "shopping"),
        ("sport,stadiums", "sports"),
        ("industrial_facilities,dams,interesting_places", "sightseeing"),
        ("", "sightseeing"),
    ],
)
def test_category_from_real_opentripmap_kinds(kinds, category):
    assert _otm_kind_to_category(kinds) == category


# ── Dev A: the tools always return coordinates ─────────────────────────────


def _mcp_settings() -> MagicMock:
    return MagicMock(DUFFEL_ACCESS_TOKEN="t", OPENTRIPMAP_API_KEY="k")


def test_attractions_without_coordinates_are_never_returned():
    places = MagicMock()
    places.json.return_value = [
        {"name": "Fort Aguada", "kinds": "historic", "rate": 3, "point": {"lat": 15.49, "lon": 73.77}},
        {"name": "Somewhere Vague", "kinds": "historic", "rate": 3},  # no point
    ]
    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _mcp_settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools._geocode", return_value=(15.3, 74.1)),
        patch("src.ai.mcp_server.tools.httpx.get", return_value=places),
    ):
        result = get_attractions(AttractionInput(destination="Goa", interests=["history"], limit=5))

    assert [a.name for a in result] == ["Fort Aguada"]
    assert all(a.lat is not None and a.lng is not None for a in result)


def test_attraction_rating_is_the_source_rate_or_zero_never_invented():
    places = MagicMock()
    places.json.return_value = [
        {"name": "Rachol Fort Gate", "kinds": "fortifications,historic", "rate": 7, "point": {"lat": 1.0, "lon": 2.0}},
        {"name": "Unrated Place", "kinds": "historic", "point": {"lat": 1.0, "lon": 2.0}},  # no rate
    ]
    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _mcp_settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools._geocode", return_value=(15.3, 74.1)),
        patch("src.ai.mcp_server.tools.httpx.get", return_value=places),
    ):
        result = get_attractions(AttractionInput(destination="Goa", interests=["history"], limit=5))

    assert {a.name: a.rating for a in result} == {"Rachol Fort Gate": 7.0, "Unrated Place": 0.0}


def test_attractions_cached_before_phase_18_are_not_served():
    """Their categories and ratings mean something else — the cache key changed with the meaning.

    And again in Phase 25 ("v3"): a search returns other places, and each says which interest it is for.
    """
    lookups: list[str] = []
    places = MagicMock()
    places.json.return_value = [
        {"name": "Fort Aguada", "kinds": "historic", "rate": 3, "point": {"lat": 1.0, "lon": 2.0}}
    ]
    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _mcp_settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", side_effect=lambda key: lookups.append(key)),
        patch("src.ai.mcp_server.tools.set_cached_sync") as store,
        patch("src.ai.mcp_server.tools._geocode", return_value=(15.3, 74.1)),
        patch("src.ai.mcp_server.tools.httpx.get", return_value=places),
    ):
        get_attractions(AttractionInput(destination="Goa", interests=["history"], limit=5))

    assert lookups and all(key.startswith("mcp:attractions:v3:") for key in lookups)
    assert store.call_args.args[0] == lookups[0]  # written where it will be read


def test_flights_carry_their_airports():
    with (
        patch("src.ai.mcp_server.tools.mcp_settings", _mcp_settings()),
        patch("src.ai.mcp_server.tools.get_cached_sync", return_value=None),
        patch("src.ai.mcp_server.tools.set_cached_sync"),
        patch("src.ai.mcp_server.tools.httpx.post", return_value=duffel_response(duffel_offer(day="2030-01-10"))),
    ):
        flight = search_flights(FlightSearchInput(origin="DEL", destination="GOI", date="2030-01-10", budget=20_000))[0]

    assert (flight.origin.code, flight.destination.code) == ("DEL", "GOI")
    assert (flight.destination.lat, flight.destination.lng) == (15.3806, 73.8332)


# ── Dev A: the save path reports what the map cannot pin ───────────────────


def test_unmapped_activities_ignores_free_time_and_lists_real_places():
    draft = _draft()
    assert _unmapped_activities(draft) == ["day 1 afternoon: Baga Beach"]  # no coordinates yet
    _attach_source_data(draft, FLIGHTS, HOTELS, ATTRACTIONS)
    assert _unmapped_activities(draft) == []


@pytest.mark.asyncio
async def test_persist_warns_but_still_saves_an_itinerary_with_unmapped_activities(caplog):
    added = []
    db = AsyncMock()
    db.get = AsyncMock(return_value=MagicMock())
    db.add = lambda obj: added.append(obj)

    async def refresh(obj):
        obj.id = uuid.uuid4()

    db.refresh = refresh
    state = {"db": db, "trip_id": uuid.uuid4(), "draft_itinerary": _draft()}

    with (
        patch("src.ai.orchestrator.orchestrator.spawn", side_effect=lambda coro: coro.close()),
        caplog.at_level(logging.WARNING, logger="src.ai.orchestrator.orchestrator"),
    ):
        result = await persist_node(state)

    assert result["itinerary_id"] is not None  # a missing coordinate never fails a trip
    assert "without coordinates" in caplog.text and "Baga Beach" in caplog.text
    persist_run = next(obj for obj in added if getattr(obj, "agent_name", None) == "persist")
    assert persist_run.output["unmapped_activities"] == ["day 1 afternoon: Baga Beach"]

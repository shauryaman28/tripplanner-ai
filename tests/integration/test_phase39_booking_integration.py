"""Phase 39 — booking links through the real tool server, and kept by a plan, on real Postgres and Redis.

RUN_INTEGRATION=1 pytest tests/integration/test_phase39_booking_integration.py -v
"""

import io
import os
from urllib.parse import parse_qs, urlsplit

import pytest
from pypdf import PdfReader

from src.ai import booking
from src.ai.mcp_client.client import call_tool
from tests.fakes import HOTELS, FakeProviders, flight, tool_server
from tests.integration.test_phase24_caching_integration import tool_cache  # noqa: F401 — the fixture
from tests.integration.test_pipeline_integration import _login, _run_finished, _stack

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)


def q(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def test_the_tool_server_answers_each_flight_and_hotel_with_its_link(tool_cache):  # noqa: F811
    async with tool_server(FakeProviders()):
        flights = await call_tool(
            "search_flights",
            {
                "origin": "DEL",
                "destination": "Goa",
                "date": "2030-01-10",
                "return_date": "2030-01-13",
                "budget": 90_000,
                "passengers": 3,
            },
        )
        hotels = await call_tool(
            "search_hotels",
            {
                "destination": "Goa",
                "check_in": "2030-01-10",
                "check_out": "2030-01-13",
                "budget_per_night": 30_000,
                "guests": 3,
            },
        )
    assert flights and all(
        q(f["booking_url"])["q"] == "Flights to GOI from DEL on 2030-01-10 through 2030-01-13 for 3 adults"
        for f in flights
    )
    assert hotels and all(
        q(h["booking_url"])["ss"] == f"{h['name']}, Goa" and q(h["booking_url"])["no_rooms"] == "2" for h in hotels
    )


async def test_a_plan_is_saved_with_its_links_and_its_pdf_can_be_clicked(db_session):
    def flights(_name, params):
        return [
            flight(8_200.0)
            | {
                "booking_url": booking.flight_link(
                    "DEL", "GOI", params["date"], params.get("return_date"), params["passengers"]
                )
            }
        ]

    def hotels(_name, params):
        return [
            h
            | {
                "booking_url": booking.hotel_link(
                    h["name"], params["destination"], params["check_in"], params["check_out"], params["guests"]
                )
            }
            for h in HOTELS
        ]

    async with _stack(flight_tool=flights, hotel_tool=hotels) as (client, _events, _tools):
        await _login(client)
        body = {
            "destination": "Goa",
            "start_date": "2030-01-10",
            "end_date": "2030-01-12",
            "budget": 60_000,
            "group_size": 2,
            "interests": ["history"],
        }
        trip_id = (await client.post("/trips", json=body)).json()["id"]
        assert (await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "A long weekend"})).status_code == 202
        await _run_finished(client, trip_id, orchestrator_rows=1)
        plan = (await client.get(f"/trips/{trip_id}/itinerary")).json()["structured_data"]
        pdf = (await client.get(f"/trips/{trip_id}/export/pdf")).content

    day = plan["days"][0]
    assert (
        q(day["flight"]["booking_url"])["q"] == "Flights to GOI from DEL on 2030-01-10 through 2030-01-12 for 2 adults"
    )
    assert q(day["hotel"]["booking_url"]) | {} == {
        "ss": "Goa Grand, Goa", "checkin": "2030-01-10", "checkout": "2030-01-12", "group_adults": "2",
        "no_rooms": "1", "group_children": "0", "selected_currency": "INR",
    }  # fmt: skip
    links = [
        str(a.get_object()["/A"]["/URI"])
        for page in PdfReader(io.BytesIO(pdf)).pages
        for a in page.get("/Annots") or []
        if a.get_object().get("/A", {}).get("/URI")
    ]
    assert links == [day["flight"]["booking_url"], day["hotel"]["booking_url"]]

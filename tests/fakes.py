"""Fakes for everything the app reaches over the network.

Duffel responses for the flight-tool tests, and `network_stubs()` — the full
set of stubs that lets the real app (FastAPI, Postgres, Redis, the LangGraph
orchestrator) plan a trip with no API keys. The integration tests and the
Playwright stub backend (tests/e2e/stub_backend.py) both run on it.
"""

import json
import re
from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from src.ai.agents.refinement_classifier import RefinementClassification


def duffel_offer(
    carrier: str = "6E",
    number: str = "204",
    amount: str = "4200.00",
    currency: str = "INR",
    day: str = "2030-01-10",
    stops: int = 0,
) -> dict:
    """One offer in the shape POST /air/offer_requests returns it."""
    segment = {
        "departing_at": f"{day}T06:00:00",
        "arriving_at": f"{day}T08:15:00",
        "marketing_carrier": {"iata_code": carrier},
        "marketing_carrier_flight_number": number,
    }
    return {
        "total_amount": amount,
        "total_currency": currency,
        "slices": [{"duration": "PT2H15M", "segments": [segment] * (stops + 1)}],
    }


def duffel_response(*offers: dict) -> MagicMock:
    """Stand-in for the httpx.Response of a successful offer request."""
    response = MagicMock()
    response.json.return_value = {"data": {"offers": list(offers)}}
    return response


def liteapi_response(*hotels: tuple[str, float], stars: int = 4) -> MagicMock:
    """Stand-in for the httpx.Response of POST /hotels/rates; each hotel is (name, total price for the stay in INR)."""
    response = MagicMock()
    response.json.return_value = {
        "hotels": [
            {
                "id": f"h{i}",
                "name": name,
                "stars": stars,
                "rating": 8.5,
                "address": "Beach Road",
                "city_name": "Goa",
                "latitude": 15.5,
                "longitude": 73.8,
            }
            for i, (name, _) in enumerate(hotels)
        ],
        "data": [
            {"hotelId": f"h{i}", "roomTypes": [{"offerRetailRate": {"amount": total, "currency": "INR"}}]}
            for i, (_, total) in enumerate(hotels)
        ],
    }
    return response


# ── Whole-pipeline stubs ───────────────────────────────────────────────────

HOTELS = [{"name": "Goa Grand", "stars": 4, "price_per_night_inr": 4500.0, "rating": 4.2, "address": "Calangute"}]
ATTRACTIONS = [
    {"name": "Fort Aguada", "category": "history", "rating": 4.5, "description": "Fort.", "lat": 15.5, "lng": 73.7}
]


_TARGETED_HOTEL = RefinementClassification(refinement_type="targeted_hotel", reason="stubbed classifier")


def flight(price: float, day: str = "2030-01-10") -> dict:
    """One search_flights result."""
    return {
        "airline": "6E",
        "flight_number": "6E-204",
        "departure": f"{day}T06:00:00",
        "arrival": f"{day}T08:15:00",
        "duration_mins": 135,
        "price_inr": price,
        "stops": 0,
    }


async def fake_builder_llm(_system: str, user_prompt: str) -> str:
    """Stand-in for the Groq call: a valid 2-day draft built only from the data in the prompt."""
    start = date.fromisoformat(re.search(r"from (\d{4}-\d{2}-\d{2}) to", user_prompt).group(1))
    flights, hotels = (
        json.loads(re.search(rf"Available {kind} \(JSON\): (\[.*?\])\n", user_prompt).group(1))
        for kind in ("flights", "hotels")
    )
    hotel = {"name": hotels[0]["name"], "cost_per_night": hotels[0]["price_per_night_inr"]} if hotels else None
    days = [
        {
            "day": n + 1,
            "date": str(start + timedelta(days=n)),
            "morning": {"activity": "Fort Aguada", "cost": 0, "lat": 15.5, "lng": 73.7},
            "afternoon": {"activity": "Explore the area", "cost": 0, "lat": None, "lng": None},
            "evening": None,
            "hotel": hotel,
            "flight": None,
        }
        for n in range(2)
    ]
    total = min((f["price_inr"] for f in flights), default=0) + sum(
        d["hotel"]["cost_per_night"] for d in days if d["hotel"]
    )
    return json.dumps({"days": days, "total_cost": total, "currency": "INR"})


@contextmanager
def network_stubs(flight_tool, hotel_tool=None):
    """Patch every network seam; yields the three MCP tool mocks keyed flight / hotel / activities.

    `flight_tool` / `hotel_tool` are called as tool(name, params) and return what
    the MCP client would: a list of results or a ToolError.
    """
    tools = {
        "flight": AsyncMock(side_effect=flight_tool),
        "hotel": AsyncMock(side_effect=hotel_tool or (lambda *_: HOTELS)),
        "activities": AsyncMock(return_value=ATTRACTIONS),
    }
    with (
        patch("src.ai.agents.flight_agent.call_tool", tools["flight"]),
        patch("src.ai.agents.hotel_agent.call_tool", tools["hotel"]),
        patch("src.ai.agents.activities_agent.call_tool", tools["activities"]),
        patch("src.ai.orchestrator.orchestrator._extract_intent", AsyncMock(return_value={})),
        patch("src.ai.builder.builder._call_llm", fake_builder_llm),
        patch("src.ai.agents.preference_extractor._call_llm", AsyncMock(side_effect=RuntimeError("no key"))),
        patch("src.ai.embeddings.embedder._call_embed", AsyncMock(return_value=[0.01] * 1536)),
        patch("app.api.routes.trips.classify_refinement", AsyncMock(return_value=_TARGETED_HOTEL)),
    ):
        yield tools

"""Fakes for everything the app reaches over the network.

Duffel responses for the flight-tool tests, and `network_stubs()` — the full
set of stubs that lets the real app (FastAPI, Postgres, Redis, the LangGraph
orchestrator) plan a trip and export it with no API keys and no network. The
integration tests and the Playwright stub backend (tests/e2e/stub_backend.py)
both run on it.
"""

import asyncio
import io
import json
import re
from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from app.core.config import settings
from src.ai.agents.refinement_classifier import RefinementClassification

_DEL = {"latitude": 28.5585, "longitude": 77.1002}
_GOI = {"latitude": 15.3806, "longitude": 73.8332}


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
        "origin": {"iata_code": "DEL", "name": "Indira Gandhi International Airport", **_DEL},
        "destination": {"iata_code": "GOI", "name": "Goa Airport", **_GOI},
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

HOTELS = [
    {
        "name": "Goa Grand",
        "stars": 4,
        "price_per_night_inr": 4500.0,
        "rating": 8.4,
        "address": "Calangute",
        "lat": 15.544,
        "lng": 73.755,
    }
]
# `rating` is OpenTripMap's popularity rate: 1–3, or 5–7 for the same scale on a heritage site.
ATTRACTIONS = [
    {"name": "Fort Aguada", "category": "history", "rating": 7.0, "description": "Fort.", "lat": 15.492, "lng": 73.773},
    {"name": "Baga Beach", "category": "beach", "rating": 2.0, "description": "Beach.", "lat": 15.556, "lng": 73.752},
    {
        "name": "Basilica of Bom Jesus",
        "category": "spiritual",
        "rating": 7.0,
        "description": "Church.",
        "lat": 15.501,
        "lng": 73.912,
    },
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
        "origin": {"code": "DEL", "name": "Indira Gandhi International Airport", "lat": 28.5585, "lng": 77.1002},
        "destination": {"code": "GOI", "name": "Goa Airport", "lat": 15.3806, "lng": 73.8332},
    }


def map_tile(colour: str = "#dfe6dc") -> bytes:
    """A plain 256 × 256 PNG — what a tile server sends for an empty patch of land."""
    out = io.BytesIO()
    Image.new("RGB", (256, 256), colour).save(out, "PNG")
    return out.getvalue()


_TILE = map_tile()

# The stub stack shares Redis with the dev servers, and tiles are cached there for a week under a
# key made from the tile server's URL. The stubs therefore answer for a tile server of their own:
# under the real one's URL, a real export would be handed these blank tiles from the cache.
STUB_TILE_URL = "https://tiles.stub.invalid/{z}/{x}/{y}.png"


async def fake_tile(_client, _url: str) -> bytes:
    """Stand-in for the tile download behind the PDF export's static map (app.pdf.static_map)."""
    return _TILE


# How the stand-in model "writes" when its reply is streamed (Phase 20): this many characters at a
# time. The pause between pieces is 0 in the tests; the Playwright stub backend sets it, so that a
# browser has something to watch.
STREAM_PIECE = 24
stream_pause = 0.0


async def stream_reply(reply: str, on_token) -> str:
    """Hand a reply over piece by piece, the way the real model's stream arrives."""
    for start in range(0, len(reply), STREAM_PIECE):
        await on_token(reply[start : start + STREAM_PIECE])
        await asyncio.sleep(stream_pause)
    return reply


async def fake_builder_llm(_system: str, user_prompt: str, on_token=None) -> str:
    """Stand-in for the Groq call: a valid draft, one entry per day of the trip, built only from the data in the prompt.

    Two attractions a day until they run out, then free days; the hotel on every
    day but the last. Like a real model it names places and leaves the
    coordinates wrong or missing — the builder attaches the real ones from the
    source data. Asked to stream (`on_token`), it hands the reply over in pieces.
    """
    start, end = (
        date.fromisoformat(d)
        for d in re.search(r"from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", user_prompt).groups()
    )
    last = (end - start).days
    flights, hotels, attractions = (
        json.loads(re.search(rf"Available {kind} \(JSON\): (\[.*?\])\n", user_prompt).group(1))
        for kind in ("flights", "hotels", "attractions")
    )
    names = [a["name"] for a in attractions]
    hotel = {"name": hotels[0]["name"], "cost_per_night": hotels[0]["price_per_night_inr"]} if hotels else None

    def slot(index: int) -> dict:
        name = names[index] if index < len(names) else "Explore the area"
        return {"activity": name, "cost": 0, "lat": None, "lng": None}

    days = [
        {
            "day": n + 1,
            "date": str(start + timedelta(days=n)),
            "morning": slot(2 * n),
            "afternoon": slot(2 * n + 1),
            "evening": None,
            "hotel": hotel if n < last else None,  # N days, N-1 nights
            "flight": None,
        }
        for n in range(last + 1)
    ]
    total = min((f["price_inr"] for f in flights), default=0) + sum(
        d["hotel"]["cost_per_night"] for d in days if d["hotel"]
    )
    reply = json.dumps({"days": days, "total_cost": total, "currency": "INR"}, indent=1)  # indented, as models write
    return await stream_reply(reply, on_token) if on_token else reply


# What the stand-in DestinationIntelligenceAgent model knows (Phase 22). Written the way a model
# writes: markdown it was told not to use, a list longer than asked for, a key nobody asked for.
# One of the best_times places is in ATTRACTIONS, so a plan made from the stubs has a tip for one of its stops.
LOCAL_TIPS = {
    "local_transport": "Rent a scooter for about ₹400 a day — the beaches are too far apart to walk between.",
    "cultural_norms": [
        "**Dress modestly** in churches and temples: shoulders and knees covered.",
        "Bargaining is expected at the flea markets, never in shops with price tags.",
    ],
    "tourist_traps": [
        "Skip the restaurants with menus in ten languages near Calangute; eat where the taxi drivers eat.",
        "Agree the taxi fare before you get in — there are no meters.",
    ],
    "best_times": {
        "Fort Aguada": "Early morning, before 9 am — it is deserted and the light is at its best.",
        "Anjuna Flea Market": "Wednesday afternoon, the only day it is held.",
    },
    "safety_tips": [f"Tip number {n}: do not leave valuables on the beach." for n in range(1, 9)],
    "sponsored_by": "nobody",
}
# Destinations the stand-in model has nothing for: no answer at all, or "I do not know this place".
TIPS_DOWN = {"Pondicherry"}
TIPS_UNKNOWN = {"Xyzzypur"}


async def fake_intelligence_llm(_system: str, user_prompt: str) -> str:
    """Stand-in for the DestinationIntelligenceAgent's Groq call: tips about the destination in the prompt."""
    destination = json.loads(user_prompt)["destination"]
    if destination in TIPS_DOWN:
        raise RuntimeError("Error code: 503 - the model is not answering")
    if destination in TIPS_UNKNOWN:
        return '{"unknown": true}'
    return "```json\n" + json.dumps(LOCAL_TIPS, ensure_ascii=False) + "\n```"  # fenced, as models like to


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
        patch("src.ai.agents.destination_intelligence._call_llm", fake_intelligence_llm),
        patch("src.ai.agents.preference_extractor._call_llm", AsyncMock(side_effect=RuntimeError("no key"))),
        patch("src.ai.embeddings.embedder._call_embed", AsyncMock(return_value=[0.01] * 1536)),
        patch("app.api.routes.trips.classify_refinement", AsyncMock(return_value=_TARGETED_HOTEL)),
        patch("app.pdf.static_map._download_tile", fake_tile),
        patch.object(settings, "MAP_TILE_URL", STUB_TILE_URL),
    ):
        yield tools

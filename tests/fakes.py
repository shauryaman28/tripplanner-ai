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
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from PIL import Image

from app import search
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
    response.json.return_value = liteapi_body(*hotels, stars=stars)
    return response


def liteapi_body(*hotels: tuple[str, float], stars: int = 4) -> dict:
    """What POST /hotels/rates answers with; each hotel is (name, total price for the stay in INR)."""
    return {
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


# What the attractions search finds in the mountains (the stub backend serves it for Leh): nothing in
# common with ATTRACTIONS, so a trip built from it is a different kind of trip (Phase 23).
MOUNTAIN_ATTRACTIONS = [
    {"name": "Pangong Lake", "category": "nature", "rating": 7.0, "description": "Lake.", "lat": 33.76, "lng": 78.67},
    {
        "name": "Thiksey Monastery",
        "category": "spiritual",
        "rating": 3.0,
        "description": "Monastery.",
        "lat": 34.06,
        "lng": 77.67,
    },
    {"name": "Khardung La", "category": "nature", "rating": 2.0, "description": "Pass.", "lat": 34.28, "lng": 77.6},
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


# ── Embeddings (Phases 14, 23) ─────────────────────────────────────────────

# Words that say nothing about what a text is about — the summary's own scaffolding among them.
_FILLER = {"a", "an", "and", "the", "to", "of", "in", "for", "trip", "kinds", "places"}

# The stand-in embedder scores on another scale than the real model, so the cut-offs that go with
# it are its own (the real ones: app/search.py). Two stub trips built from the same attractions
# score about 0.9, two built from different ones under 0.1; a one-word query scores 0.2–0.7
# against a trip that has the word and 0 against one that has not.
STUB_SIMILAR_FLOOR, STUB_SEARCH_FLOOR, STUB_SEARCH_WINDOW = 0.5, 0.15, 0.3
# The search keeps each query's vector in Redis under the embedding model's name — and the test
# stacks share Redis with the dev servers. A name of this process's own keeps the stand-in's vectors
# away from the real model's, and from another run's (which numbered its words differently).
STUB_EMBEDDING_MODEL = f"stub-words-{uuid.uuid4().hex[:8]}"


# Each word's own dimension, in the order words are first seen. (Hashing words into 1536 dimensions
# made unrelated words collide now and then: "zzzz" matched a trip.) One process embeds both the
# stored texts and the queries, so the numbering only has to hold for as long as it runs.
_dimension: dict[str, int] = {}


def word_vector(text: str) -> list[float]:
    """A stand-in embedding: one dimension per word, so two texts are as close as the words they share."""
    vector = [0.0] * 1536
    for word in re.findall(r"[a-z]+", text.lower()):
        if word not in _FILLER:
            vector[_dimension.setdefault(word, len(_dimension) % 1536)] += 1.0
    if not any(vector):
        vector[1535] = 1.0  # no words at all: still a direction (a zero vector has no cosine)
    return vector


async def fake_embed(text: str, _task_type: str | None = None) -> list[float]:
    """Stand-in for the Gemini embedding call, for a stored text and a typed query alike."""
    return word_vector(text)


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
    """Patch every network seam; yields the MCP tool mocks keyed flight / hotel / activities / warm.

    `flight_tool` / `hotel_tool` are called as tool(name, params) and return what
    the MCP client would: a list of results or a ToolError. "warm" is every call
    cache warming makes (Phase 24) — its own mock, so that warming a trip never
    counts as one of the plan's searches.
    """
    tools = {
        "flight": AsyncMock(side_effect=flight_tool),
        "hotel": AsyncMock(side_effect=hotel_tool or (lambda *_: HOTELS)),
        "activities": AsyncMock(return_value=ATTRACTIONS),
        "warm": AsyncMock(return_value=[]),
    }
    with (
        patch("src.ai.agents.flight_agent.call_tool", tools["flight"]),
        patch("src.ai.agents.hotel_agent.call_tool", tools["hotel"]),
        patch("src.ai.agents.activities_agent.call_tool", tools["activities"]),
        patch("src.ai.orchestrator.warming.call_tool", tools["warm"]),
        patch("src.ai.orchestrator.orchestrator._extract_intent", AsyncMock(return_value={})),
        patch("src.ai.builder.builder._call_llm", fake_builder_llm),
        patch("src.ai.agents.destination_intelligence._call_llm", fake_intelligence_llm),
        patch("src.ai.agents.preference_extractor._call_llm", AsyncMock(side_effect=RuntimeError("no key"))),
        patch("src.ai.embeddings.embedder._call_embed", AsyncMock(side_effect=fake_embed)),
        patch("app.api.routes.trips.embed_query", AsyncMock(side_effect=fake_embed)),
        patch("app.api.routes.trips.EMBEDDING_MODEL", STUB_EMBEDDING_MODEL),
        patch.object(search, "SIMILAR_FLOOR", STUB_SIMILAR_FLOOR),
        patch.object(search, "SEARCH_FLOOR", STUB_SEARCH_FLOOR),
        patch.object(search, "SEARCH_WINDOW", STUB_SEARCH_WINDOW),
        patch("app.api.routes.trips.classify_refinement", AsyncMock(return_value=_TARGETED_HOTEL)),
        patch("app.pdf.static_map._download_tile", fake_tile),
        patch.object(settings, "MAP_TILE_URL", STUB_TILE_URL),
    ):
        yield tools


# ── The providers themselves, behind the real tools (Phase 24) ─────────────
#
# network_stubs() replaces the tools. These replace only what the tools talk to — httpx.get and
# httpx.post — so that the real MCP server, its threads, its cache keys, its rate limits and its
# backoff are what a test runs.


def provider_answer(status: int, body=None, headers: dict | None = None, url: str = "https://provider.test/"):
    """A real httpx.Response: its raise_for_status() raises exactly as a provider's error would."""
    return httpx.Response(status, json={} if body is None else body, headers=headers, request=httpx.Request("GET", url))


class FakeProviders:
    """Stand-ins for httpx.get / httpx.post that answer as the five providers do. Safe to call from threads.

    requests      the provider of every request received, in order
    answers       (provider, status) of every answer given
    turn_away     {provider: [statuses]} — answered first, one per request: {"duffel": [429, 429]}
    limits        {provider: (calls, seconds)} — the provider then counts as a real one does, in real
                  time, and answers 429 past its limit
    on_request    called with the provider's name while a request is "at the provider" — a place to
                  hold it (an Event, a Barrier) or to slow it down
    """

    HOSTS = {
        "api.duffel.com": "duffel",
        "api.liteapi.travel": "liteapi",
        "api.opentripmap.com": "opentripmap",
        "api.openweathermap.org": "openweather",
        "nominatim.openstreetmap.org": "nominatim",
    }

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.answers: list[tuple[str, int]] = []
        self.turn_away: dict[str, list[int]] = {}
        self.limits: dict[str, tuple[int, float]] = {}
        self.on_request: Callable[[str], None] | None = None
        self.offers = [duffel_offer(amount="4200.00"), duffel_offer(carrier="AI", number="101", amount="6100.00")]
        self.hotels: list[tuple[str, float]] = [("Goa Grand", 18_000.0), ("Sea Breeze", 26_000.0)]  # for the stay
        self._arrivals: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def get(self, url: str, **_request) -> httpx.Response:
        return self._answer(url)

    def post(self, url: str, **_request) -> httpx.Response:
        return self._answer(url)

    def counts(self) -> Counter:
        """How many requests each provider has received."""
        with self._lock:
            return Counter(self.requests)

    @property
    def turned_away(self) -> list[tuple[str, int]]:
        """Every answer that was a 429."""
        with self._lock:
            return [answer for answer in self.answers if answer[1] == 429]

    def _answer(self, url: str) -> httpx.Response:
        provider = self.HOSTS[httpx.URL(url).host]
        with self._lock:
            self.requests.append(provider)
            status = 200
            if self.turn_away.get(provider):
                status = self.turn_away[provider].pop(0)
            elif provider in self.limits:
                calls, period = self.limits[provider]
                now = time.monotonic()
                recent = [at for at in self._arrivals.get(provider, []) if now - at < period]
                if len(recent) >= calls:
                    status = 429
                else:
                    recent.append(now)
                self._arrivals[provider] = recent
            self.answers.append((provider, status))
        if self.on_request is not None:
            self.on_request(provider)
        return provider_answer(status, self._body(provider) if status == 200 else None, url=url)

    def _body(self, provider: str):
        if provider == "duffel":
            return {"data": {"offers": self.offers}}
        if provider == "liteapi":
            return liteapi_body(*self.hotels)
        if provider == "opentripmap":
            return [
                {
                    "name": "Fort Aguada",
                    "kinds": "historic,fortifications",
                    "rate": 7,
                    "point": {"lat": 15.49, "lon": 73.77},
                },
                {"name": "Baga Beach", "kinds": "beaches,natural", "rate": 2, "point": {"lat": 15.55, "lon": 73.75}},
            ]
        if provider == "nominatim":
            place = {"class": "boundary", "importance": 0.7, "lat": "15.30", "lon": "74.08"}
            return [{**place, "address": {"country_code": "in", "country": "India"}}]
        reading = {"dt": int(time.time()), "weather": [{"main": "Clear"}], "main": {"temp_max": 31.0, "temp_min": 24.0}}
        return {"list": [reading]}


@contextmanager
def memory_cache():
    """The tools' Redis cache as a dict: yields {key: (value, ttl in seconds)}."""
    store: dict[str, tuple] = {}

    def read(key: str):
        return json.loads(json.dumps(store[key][0])) if key in store else None

    def write(key: str, value, ttl: int = 900) -> None:
        store[key] = (json.loads(json.dumps(value, default=str)), ttl)

    with (
        patch("src.ai.mcp_server.tools.get_cached_sync", read),
        patch("src.ai.mcp_server.tools.set_cached_sync", write),
    ):
        yield store


@contextmanager
def providers_faked(providers: FakeProviders):
    """Every provider the tools reach is `providers`, and every API key is set."""
    keys = MagicMock(DUFFEL_ACCESS_TOKEN="t", LITEAPI_API_KEY="k", OPENTRIPMAP_API_KEY="k", OPENWEATHER_API_KEY="k")
    with (
        patch("src.ai.mcp_server.tools.httpx.get", providers.get),
        patch("src.ai.mcp_server.tools.httpx.post", providers.post),
        patch("src.ai.mcp_server.tools.mcp_settings", keys),
    ):
        yield providers


@asynccontextmanager
async def tool_server(providers: FakeProviders | None = None):
    """The real MCP server and the real client, in this process, in front of fake providers.

    Inside it call_tool() — the agents' and the cache warmer's — runs the client's
    own code against the real FastMCP server object: threaded tools, cache keys,
    rate limits, backoff. Only the transport is in memory instead of a
    subprocess's pipes, and only httpx.get / httpx.post are fakes.

    Use it as `async with` inside the test itself, not as a fixture: the session
    must be closed by the task that opened it.
    """
    from mcp.shared.memory import create_connected_server_and_client_session

    from src.ai.mcp_server.server import mcp

    async with create_connected_server_and_client_session(mcp) as session:
        with (
            providers_faked(providers or FakeProviders()) as faked,
            patch("src.ai.mcp_client.client.get_session", AsyncMock(return_value=session)),
        ):
            yield faked

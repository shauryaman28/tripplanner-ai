"""Backend for the Playwright smoke test.

The real app — FastAPI, Postgres, Redis, the LangGraph orchestrator, SSE —
with only the external APIs stubbed (tests/fakes.py), so the browser flow is
deterministic and needs no API keys. It is a separate stack from the dev one:
its own port and its own `<db>_e2e` database, emptied on every start, so it can
run while the dev servers are up and never touches dev data.

Run from the repo root (Playwright does this itself — src/frontend/playwright.config.ts):

    python -m tests.e2e.stub_backend        # serves on http://localhost:8100

What the stubbed providers do depends on the destination, so one test can
drive one situation:

    Udaipur (or any budget the flights eat up)   ₹8,200 direct, ₹2,500 with a connection
    Jaisalmer                                    ₹30,000 direct (₹2,500 with a connection): on ₹42,000 a conflict
                                                 whose shorter trip and 3-star stay both fit (Phase 21)
    Hampi, Badami                                the hotel search is down; it answers when retried
    Kaza                                         no airport is known for it: the flight search fails, every time
    Leh                                          the attractions are a lake, a monastery and a pass: a trip that is
                                                 not like the others, for similar trips and search (Phase 23)
    Pondicherry                                  the local-tips model does not answer: a plan without tips (Phase 22)
    Gokarna                                      no local tips with the plan; they come with the first change to it
    Shimla, Manali                               every search is down; they answer when the trip is retried
    anywhere else                                everything is found; a second hotel search finds a different hotel

"Down, then fine" and "one hotel, then another" both go by how often the same
search has been made: odd times one answer, even times the other. A test makes
its searches in pairs (plan → retry, plan → refine) and uses dates of its own,
so tests do not disturb each other and a stub server left running behaves the
same on the next run.
"""

import json
import os
import sys
from collections import Counter
from unittest.mock import patch

import uvicorn

os.environ.setdefault("APP_ENV", "test")  # no SQL echo
sys.path.insert(0, "src/backend")

from tests import fakes  # noqa: E402
from tests.database import create_and_migrate, empty_all_tables, use_database  # noqa: E402
from tests.fakes import ATTRACTIONS, HOTELS, MOUNTAIN_ATTRACTIONS, flight, network_stubs  # noqa: E402

PORT = int(os.getenv("PORT", "8100"))

# The itinerary is streamed to the page while it is written (Phase 20). The stand-in model would
# be done before a browser could draw anything, so here it pauses between pieces: about half a
# second for a three-day plan.
STREAM_PAUSE = float(os.getenv("STREAM_PAUSE", "0.015"))

if __name__ == "__main__":
    use_database("_e2e")  # before the app is imported: it builds its DB engine at import time
    create_and_migrate()
    empty_all_tables()
    fakes.stream_pause = STREAM_PAUSE

    from src.ai.mcp_server.models import ToolError

    HOTEL_DOWN = {"Hampi", "Badami"}
    ALL_DOWN = {"Shimla", "Manali"}
    NO_AIRPORT = {"Kaza"}  # in Spiti: there is none
    DEAR_FLIGHTS = {"Jaisalmer": 30_000.0}

    # How often each search has been made (see the module docstring).
    searches: Counter[str] = Counter()

    def first_of_a_pair(kind: str, params: dict, *keys: str) -> bool:
        key = "|".join([kind, *(str(params.get(k)) for k in keys)])
        searches[key] += 1
        return searches[key] % 2 == 1

    def down(what: str) -> ToolError:
        return ToolError(error=f"The {what} search is not answering (HTTP 503).", code="PROVIDER_ERROR")

    def flight_tool(_name: str, params: dict):
        if params.get("destination") in NO_AIRPORT:  # what the real tool says for a city it has no airport for
            return ToolError(
                error=f"Unknown airport: '{params['destination']}'. Pass an IATA code or add the city to _CITY_IATA in tools.py.",
                code="UNKNOWN_DESTINATION",
            )
        if params.get("destination") in ALL_DOWN and first_of_a_pair("flights", params, "destination", "date"):
            return down("flight")
        # ₹8,200 direct; a re-plan that allows connections ("cheaper flights") finds ₹2,500.
        direct = DEAR_FLIGHTS.get(params.get("destination"), 8_200.0)
        return [flight(2_500.0 if params.get("max_stops", 1) >= 2 else direct)]

    def hotel_tool(_name: str, params: dict):
        first = first_of_a_pair("hotels", params, "destination", "check_in", "guests")
        if params.get("destination") in HOTEL_DOWN | ALL_DOWN:
            return down("hotel") if first else HOTELS
        # Searching hotels for the same stay again (a refinement) finds a different one at the same
        # price, so a refinement visibly changes the plan.
        if first:
            return HOTELS
        return [{**HOTELS[0], "name": "Baga Beach House", "stars": 5, "rating": 9.1}, *HOTELS]

    def activities_tool(_name: str, params: dict):
        if params.get("destination") in ALL_DOWN and first_of_a_pair("activities", params, "destination"):
            return down("attraction")
        return MOUNTAIN_ATTRACTIONS if params.get("destination") == "Leh" else ATTRACTIONS

    TIPS_LATE = {"Gokarna"}

    async def tips_model(system: str, user_prompt: str) -> str:
        """The DestinationIntelligenceAgent's model (Phase 22): tests/fakes.py's, late for some destinations."""
        facts = json.loads(user_prompt)
        if facts["destination"] in TIPS_LATE and first_of_a_pair("tips", facts, "destination", "month"):
            raise RuntimeError("Error code: 503 - the model is not answering")
        return await fakes.fake_intelligence_llm(system, user_prompt)

    with (
        network_stubs(flight_tool=flight_tool, hotel_tool=hotel_tool) as tools,
        patch("src.ai.agents.destination_intelligence._call_llm", tips_model),
    ):
        tools["activities"].side_effect = activities_tool
        from app.main import app

        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")

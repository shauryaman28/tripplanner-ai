"""Backend for the Playwright smoke test.

The real app — FastAPI, Postgres, Redis, the LangGraph orchestrator, SSE —
with only the external APIs stubbed (tests/fakes.py), so the browser flow is
deterministic and needs no API keys. It is a separate stack from the dev one:
its own port and its own `<db>_e2e` database, emptied on every start, so it can
run while the dev servers are up and never touches dev data.

Run from the repo root (Playwright does this itself — src/frontend/playwright.config.ts):

    python -m tests.e2e.stub_backend        # serves on http://localhost:8100
"""

import os
import sys
from collections import Counter

import uvicorn

os.environ.setdefault("APP_ENV", "test")  # no SQL echo
sys.path.insert(0, "src/backend")

from tests.database import create_and_migrate, empty_all_tables, use_database  # noqa: E402
from tests.fakes import HOTELS, flight, network_stubs  # noqa: E402

PORT = int(os.getenv("PORT", "8100"))

if __name__ == "__main__":
    use_database("_e2e")  # before the app is imported: it builds its DB engine at import time
    create_and_migrate()
    empty_all_tables()

    # ₹8,200 direct; a re-plan that allows connections ("cheaper flights") finds ₹2,500.
    def flight_tool(_name: str, params: dict) -> list[dict]:
        return [flight(2_500.0 if params.get("max_stops", 1) >= 2 else 8_200.0)]

    # Searching hotels for the same stay again (a refinement) finds a different one at the same
    # price, so a refinement visibly changes the plan. Searches alternate, so a stub server left
    # running behaves the same on the next test run.
    searches: Counter[str] = Counter()

    def hotel_tool(_name: str, params: dict) -> list[dict]:
        stay = f"{params.get('destination')}|{params.get('check_in')}|{params.get('guests')}"
        searches[stay] += 1
        if searches[stay] % 2:
            return HOTELS
        return [{**HOTELS[0], "name": "Baga Beach House", "stars": 5, "rating": 9.1}, *HOTELS]

    with network_stubs(flight_tool=flight_tool, hotel_tool=hotel_tool):
        from app.main import app

        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")

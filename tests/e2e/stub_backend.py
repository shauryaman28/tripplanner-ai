"""Backend for the Playwright smoke test.

The real app — FastAPI, Postgres, Redis, the LangGraph orchestrator, SSE —
with only the external APIs stubbed (tests/fakes.py), so the browser flow is
deterministic and needs no API keys. Run from the repo root:

    python -m tests.e2e.stub_backend        # serves on http://localhost:8000

Playwright starts it automatically (src/frontend/playwright.config.ts) unless
something is already listening on :8000 — e.g. the real backend with real keys.
"""

import sys

import uvicorn

sys.path.insert(0, "src/backend")

from tests.fakes import flight, network_stubs  # noqa: E402

if __name__ == "__main__":
    with network_stubs(flight_tool=lambda *_: [flight(8_200.0)]):
        from app.main import app

        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")

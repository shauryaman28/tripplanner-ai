"""Point the app at a dedicated `<db>_test` database for integration runs.

This must run before `app.db.session` is imported (it builds its engine at
import time), so the integration fixtures can truncate tables freely without
ever touching dev data.
"""

import os

if os.getenv("RUN_INTEGRATION"):
    from tests.database import use_database

    use_database("_test")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def no_fallback_llm():
    """A test whose Gemini mock fails must not reach a real model through the Groq fallback (src.ai.llm.ask)."""
    with patch("src.ai.llm._ask_groq", AsyncMock(side_effect=RuntimeError("no fallback model in tests"))) as groq:
        yield groq


@pytest.fixture(autouse=True)
def no_destination_intelligence_llm():
    """The DestinationIntelligenceAgent (Phase 22) runs in every plan: a test that wants local tips stubs its model.

    Without this, any test that drives the orchestrator would ask a real model.
    Failing here is the agent's quiet path — the plan is made without tips.
    """
    refuse = AsyncMock(side_effect=RuntimeError("no model in tests"))
    with patch("src.ai.agents.destination_intelligence._call_llm", refuse) as llm:
        yield llm


@pytest.fixture(autouse=True)
def no_tile_downloads():
    """The PDF export draws its map from tiles: a test stubs them (tests/fakes.fake_tile), it never fetches any."""
    refuse = AsyncMock(side_effect=RuntimeError("no tile server in tests"))
    with patch("app.pdf.static_map._download_tile", refuse) as download:
        yield download

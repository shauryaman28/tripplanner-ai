"""Point the app at a dedicated `<db>_test` database for integration runs.

This must run before `app.db.session` is imported (it builds its engine at
import time), so the integration fixtures can truncate tables freely without
ever touching dev data.
"""

import os

# Phase 29: the suites say how they log and that they send no traces, whatever .env says. A developer
# who has LOG_FORMAT=json or a trace endpoint in .env to look at their own stack must not find the
# tests writing JSON over pytest's output, or a few thousand test requests in their Jaeger. (Before
# the app is imported: it sets up its logging as it is loaded.)
os.environ["LOG_FORMAT"] = "text"
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

if os.getenv("RUN_INTEGRATION"):
    from tests.database import use_database

    use_database("_test")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

from src.ai.embeddings.embedder import EmbeddingNotConfigured  # noqa: E402


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
def no_embedding_calls():
    """Embeddings (Phases 14, 23) come from a model: a test stubs it (tests/fakes.fake_embed), it never calls one.

    `_embed_once` is the one function that reaches the network, behind both a
    stored text (`_call_embed`, which would retry for a minute) and a typed
    query (`embed_query`).
    """
    refuse = AsyncMock(side_effect=EmbeddingNotConfigured("no embedding model in tests"))
    with patch("src.ai.embeddings.embedder._embed_once", refuse) as embed:
        yield embed


@pytest.fixture(autouse=True)
def no_waiting():
    """A rate limit or a backoff (Phase 24) waits in real time: a test gets the waits as a list, and no test sits through one.

    Yields [("limit" | "backoff", seconds), …] in the order the waits were asked for.
    The limiters also count from one call to the next — each test starts from
    none, or the thirty-first flight search of a test run would be held a minute.
    """
    from src.ai.mcp_server import rate_limiter

    rate_limiter.reset()
    waits: list[tuple[str, float]] = []
    with (
        patch("src.ai.mcp_server.rate_limiter._sleep", lambda seconds: waits.append(("limit", seconds))),
        patch("src.ai.mcp_server.outbound._sleep", lambda seconds: waits.append(("backoff", seconds))),
    ):
        yield waits
    rate_limiter.reset()


@pytest.fixture(autouse=True)
def no_cache_warming():
    """Creating a trip starts its searches in the background (Phase 24): a test that wants them starts them itself.

    Left running, the task would outlive the test that created the trip — and
    open a database session of its own, on whatever database is configured.
    """
    with patch("app.api.routes.trips.start_cache_warming") as start:
        yield start


@pytest.fixture(autouse=True)
def costs_as_shipped():
    """The over-cost limit and the price table (Phase 26) are the code's own in every test, whatever .env says.

    Someone who corrects a price in .env (LLM_PRICES), or lowers the limit to
    see the alert fire (LLM_COST_ALERT_USD), must not find the cost tests failing.
    """
    from app.core.config import Settings, settings

    with (
        patch.object(settings, "LLM_COST_ALERT_USD", Settings.model_fields["LLM_COST_ALERT_USD"].default),
        patch.object(settings, "LLM_PRICES", {}),
    ):
        yield


@pytest.fixture(autouse=True)
def no_error_reporting():
    """Sentry (Phase 27) is off in every test that does not switch it on: a DSN in .env must not be sent a
    report for every error a test raises on purpose."""
    from app.core import error_reporting

    error_reporting.switch_off()
    yield
    error_reporting.switch_off()


@pytest.fixture(autouse=True)
def no_tracing():
    """LangSmith tracing (Phase 26) is off in every test that does not switch it on (tests/fakes.traced).

    Off is said to the tracing library too: a `LANGSMITH_TRACING=true` in the
    shell that runs the tests would otherwise upload every stubbed plan to
    whatever project that shell's key belongs to.
    """
    from src.ai import tracing

    tracing.use(None)
    yield
    tracing.use(None)


@pytest.fixture(autouse=True)
def no_tile_downloads():
    """The PDF export draws its map from tiles: a test stubs them (tests/fakes.fake_tile), it never fetches any."""
    refuse = AsyncMock(side_effect=RuntimeError("no tile server in tests"))
    with patch("app.pdf.static_map._download_tile", refuse) as download:
        yield download


@pytest.fixture
def spans():
    """Every span that ends during the test (Phase 29): `spans.get_finished_spans()`.

    Nothing is sent anywhere: the spans are kept in a list. Afterwards spans go to nobody again.
    """
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from src.telemetry import traces

    kept = InMemorySpanExporter()
    traces.use(SimpleSpanProcessor(kept))
    yield kept
    traces.use(None)

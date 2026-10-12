"""Phase 29 — one trace from the request to the row, on a real Postgres, a real Redis and a tool server in another process.

    RUN_INTEGRATION=1 pytest tests/integration/test_phase29_observability_integration.py -v

The unit tests hold what the app's own code does. These hold what the libraries' instrumentation does
with the real servers, and what happens at the one place a trace leaves the process:

- a planning request is the root of one trace that reaches every SQL statement and Redis command of
  the run it started, down to the INSERT of the itinerary — with no span left without its parent,
  and with nothing of what was stored in what was recorded;
- the pieces of an itinerary published while it is written are not spans, and a health check is not
  a trace;
- every engine the app makes reports its statements, not only the first;
- a call to the tool server — started here as it is in production: a process of its own, spoken to
  over its pipes — is a span of the caller's trace over there, its log lines carry the caller's
  trip, and what its cache did comes back and is counted here.

The models and the providers are stand-ins, as in every test; nothing leaves the machine. The whole
of it against real providers, with Prometheus, Jaeger and Loki reading: scripts/observability_check.py.
"""

import asyncio
import json
import os
import re
import threading
import uuid
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import redis as sync_redis
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.trace import SpanKind
from prometheus_client import REGISTRY
from sqlalchemy import text

from app.db.session import make_engine
from src.ai.mcp_client import client as mcp_client
from src.ai.mcp_client.client import call_tool, close_session
from src.ai.mcp_server import cache as tool_cache
from src.ai.mcp_server.config import mcp_settings
from src.ai.mcp_server.models import ToolError
from src.telemetry import context, traces
from tests.fakes import FakeProviders, flight, tool_server
from tests.integration.test_pipeline_integration import _create_trip, _login, _run_finished, _stack

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

PLAN_REQUEST = "POST /trips/{trip_id}/plan"


def metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def under(span, name: str, by_id: dict) -> bool:
    while span.parent is not None and span.parent.span_id in by_id:
        span = by_id[span.parent.span_id]
        if span.name == name:
            return True
    return False


# ── One trace, from the request to the row ─────────────────────────────────


async def test_one_planning_request_is_one_trace_from_the_request_to_the_row(db_session, spans):
    plans = metric("http_requests_total", handler="/trips/{trip_id}/plan", method="POST", status="2xx")
    runs = metric("planning_run_duration_seconds_count", kind="plan", outcome="completed")
    builds = metric("itinerary_builds_total", status="success")

    async with _stack(lambda *_: [flight(8_200.0)]) as (client, events, _tools):
        await _login(client)
        trip_id = await _create_trip(client)
        user_id = (await client.get(f"/trips/{trip_id}")).json()["user_id"]
        spans.clear()

        assert (await client.post(f"/trips/{trip_id}/plan", json={})).status_code == 202
        await _run_finished(client, trip_id, 1)
        await asyncio.sleep(0.3)  # the run's last span ends a moment after its last row is written
        shown = (await client.get("/metrics")).text

    made = spans.get_finished_spans()
    (root,) = [span for span in made if span.name == PLAN_REQUEST]
    trace = [span for span in made if span.context.trace_id == root.context.trace_id]
    by_id = {span.context.span_id: span for span in trace}

    # one trace, one root, nobody left without a parent
    assert root.parent is None and root.kind is SpanKind.SERVER
    assert [span.name for span in trace if span.parent is None] == [PLAN_REQUEST]
    assert [span.name for span in trace if span.parent is not None and span.parent.span_id not in by_id] == []
    (run,) = [span for span in trace if span.name == "planning run"]
    assert (
        run.parent.span_id == root.context.span_id and run.end_time > root.end_time
    )  # the request's child; outlives it

    # FastAPI → SQLAlchemy → Redis, as the libraries' own instrumentation writes them
    statements = [span for span in trace if span.attributes.get("db.system") == "postgresql"]
    commands = [span for span in trace if span.attributes.get("db.system") == "redis"]
    assert len(statements) >= 10 and len(commands) >= 3
    in_the_request = {
        span.name for span in trace if span.parent is not None and span.parent.span_id == root.context.span_id
    }
    assert {
        "SELECT",
        "UPDATE",
        "PUBLISH",
        "planning run",
    } <= in_the_request  # the login, the trip, the announcement, the run
    # …to the row: the itinerary's INSERT is under the span that saves the plan, which is under the run
    saved = [
        span
        for span in statements
        if str(span.attributes.get("db.statement", "")).startswith("INSERT INTO itineraries")
    ]
    assert len(saved) == 1 and under(saved[0], "persist", by_id) and under(saved[0], "planning run", by_id)
    for agent in (
        "orchestrator",
        "intent_parsing",
        "flight_agent",
        "hotel_agent",
        "activities_agent",
        "itinerary_builder",
        "persist",
    ):
        assert any(span.name == agent and under(span, "planning run", by_id) for span in trace), agent

    # every span says whose it is — the libraries' spans too
    assert {span.attributes.get("trip.id") for span in trace} == {trip_id}
    assert {span.attributes.get("user.id") for span in trace} == {user_id}

    # what was recorded is what was done, not what was stored
    recorded = json.dumps([dict(span.attributes) for span in trace], default=str)
    assert (
        "$1" in recorded and "@example.com" not in recorded and "testpass123" not in recorded
    )  # SQL without its values
    for command in commands:  # a command's name, never its arguments: the keys hold ids, the values hold the plan
        assert re.fullmatch(r"[A-Z]+( \?)*", command.attributes["db.statement"]), command.attributes["db.statement"]

    # the pieces of the itinerary were published as it was written — and are not spans
    pieces = [event for event in events if event.get("event") == "builder_token"]
    published = [span for span in trace if span.name == "PUBLISH"]
    assert len(pieces) >= 2 and len(published) == len(events) - len(pieces)

    # and the same request, counted
    assert metric("http_requests_total", handler="/trips/{trip_id}/plan", method="POST", status="2xx") - plans == 1
    assert metric("planning_run_duration_seconds_count", kind="plan", outcome="completed") - runs == 1
    assert metric("itinerary_builds_total", status="success") - builds == 1
    assert (
        'http_requests_total{handler="/trips/{trip_id}/plan",method="POST",status="2xx"}' in shown
        and trip_id not in shown
    )


async def test_a_health_check_on_the_real_services_is_answered_and_starts_no_trace(db_session, spans):
    async with _stack(lambda *_: []) as (client, _events, _tools):
        ready = await client.get("/ready")
        ping = await client.get("/ping")
        metrics_read = await client.get("/metrics")

    assert (ready.status_code, ping.status_code, metrics_read.status_code) == (200, 200, 200)
    assert ready.json() == {"postgres": "ok", "redis": "ok"}  # it did ask both…
    assert spans.get_finished_spans() == ()  # …and neither question was recorded: asked every few seconds, for ever


async def test_every_engine_the_app_makes_reports_its_statements(db_session, spans):
    from app.core.config import settings

    another = make_engine(settings.DATABASE_URL)  # as a second instance would make one
    try:
        with traces.span("a request"):
            async with another.connect() as connection:
                assert (await connection.execute(text("SELECT 41 + 1"))).scalar() == 42
    finally:
        await another.dispose()

    asked = [span for span in spans.get_finished_spans() if span.attributes.get("db.statement") == "SELECT 41 + 1"]
    assert len(asked) == 1 and asked[0].attributes["db.system"] == "postgresql"
    assert asked[0].context.trace_id == spans.get_finished_spans()[-1].context.trace_id  # part of the request's trace


# ── The tool server, in a process of its own ───────────────────────────────


class Collector:
    """A place for another process to send its spans: OTLP over HTTP, kept in a list. Nothing is forwarded."""

    def __init__(self) -> None:
        self.spans: list[dict] = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — the name http.server calls
                sent = ExportTraceServiceRequest.FromString(self.rfile.read(int(self.headers["Content-Length"])))
                for resource in sent.resource_spans:
                    service = next(
                        a.value.string_value for a in resource.resource.attributes if a.key == "service.name"
                    )
                    for scope in resource.scope_spans:
                        for span in scope.spans:
                            collector.spans.append(
                                {
                                    "service": service,
                                    "name": span.name,
                                    "trace": span.trace_id.hex(),
                                    "id": span.span_id.hex(),
                                    "parent": span.parent_span_id.hex(),
                                    "kind": span.kind,
                                    "attributes": {
                                        a.key: a.value.string_value or a.value.int_value for a in span.attributes
                                    },
                                }
                            )
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def named(self, name: str) -> list[dict]:
        return [span for span in self.spans if span["name"] == name]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


async def test_a_call_to_the_tool_server_in_another_process_is_part_of_the_callers_trace(spans, monkeypatch, capfd):
    day = date.today() + timedelta(days=40)
    search = {
        "origin": "DEL",
        "destination": "GOI",
        "date": str(day),
        "return_date": str(day + timedelta(days=3)),
        "budget": 50_000.0,
    }
    trip_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    store = sync_redis.from_url(mcp_settings.REDIS_URL, decode_responses=True)
    before_keys = set(store.keys("mcp:flights:*"))

    # The answer is put in the cache first — by the real tools, in this process, in front of a stand-in
    # provider — so that the process started below can answer from Redis and needs nobody else.
    tool_cache.reset_client()
    async with tool_server(FakeProviders()):
        seeded = await call_tool("search_flights", search)
    assert not isinstance(seeded, ToolError) and len(seeded) == 2
    (key,) = set(store.keys("mcp:flights:*")) - before_keys

    collector = Collector()
    await close_session()  # whatever session an earlier test left: the one below is started with this environment
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.address)
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "not-a-key")  # a key it never gets to use: the answer is cached…
    for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(proxy, "http://127.0.0.1:9")  # …and could not: every way out of the process is shut
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    hits = metric("mcp_cache_lookups_total", cache="flights", result="hit")
    capfd.readouterr()
    try:
        with context.scope(trip_id=trip_id, user_id=user_id), traces.span("flight_agent") as agent:
            answered = await call_tool(
                "search_flights", search
            )  # starts the tool server: python -m src.ai.mcp_server.server
        assert answered == seeded, answered

        for _ in range(100):  # the other process sends its finished spans every two seconds
            if collector.named("tools/call search_flights") and collector.named("GET"):
                break
            await asyncio.sleep(0.1)
    finally:
        await close_session()
        tool_cache.reset_client()
        store.delete(key)
        store.close()
        collector.close()

    trace_id = format(agent.get_span_context().trace_id, "032x")
    of_this_call = [span for span in spans.get_finished_spans() if format(span.context.trace_id, "032x") == trace_id]
    (sent,) = [span for span in of_this_call if span.name == "mcp.call search_flights"]
    (answered_there,) = collector.named("tools/call search_flights")
    (looked_up,) = collector.named("GET")

    # one trace, two processes: the call that left this one is the parent of the call answered in that one
    assert answered_there["service"] == "tripplanner-mcp" and answered_there["trace"] == trace_id
    assert answered_there["parent"] == format(sent.context.span_id, "016x")
    assert looked_up["trace"] == trace_id and looked_up["parent"] == answered_there["id"]  # its cache lookup, under it
    assert looked_up["attributes"]["db.statement"] == "GET ?"  # the command, never the key
    assert {span["service"] for span in collector.spans} == {"tripplanner-mcp"}
    assert not collector.named("provider duffel")  # nobody was asked: it came from the cache
    # whose the work was went with the call
    for span in (answered_there, looked_up):
        assert (span["attributes"]["trip.id"], span["attributes"]["user.id"]) == (trip_id, user_id)
    # what the cache did came back with the answer, and is counted in this process
    assert metric("mcp_cache_lookups_total", cache="flights", result="hit") - hits == 1

    # and the other process's own log lines — its standard error — say whose they are
    written = [json.loads(line) for line in capfd.readouterr().err.splitlines() if line.startswith("{")]
    hit = [line for line in written if line["message"].startswith("[CACHE HIT]")]
    assert len(hit) == 1 and hit[0]["service"] == "tripplanner-mcp" and hit[0]["logger"] == "src.ai.mcp_server.cache"
    assert (hit[0]["trip_id"], hit[0]["user_id"], hit[0]["trace_id"]) == (trip_id, user_id, trace_id)
    assert all(line["service"] == "tripplanner-mcp" for line in written)
    assert mcp_client._session is None

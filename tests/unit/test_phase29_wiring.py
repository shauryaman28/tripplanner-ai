"""Phase 29 — where the app, the planner and the tool server say what they are doing.

src/telemetry on its own is tests/unit/test_phase29_telemetry.py. Here: that a request says whose it
is before anything handles it; that the planning run it starts is timed, counted and one span under
it; that every agent's work and every model call is a span, counted for the agent that asked; and
that a call to the tool server — another process — carries the trace and the trip with it and
brings back what the cache did.

The tool server here is the real server object and the real client, with the transport in memory
(tests/fakes.tool_server). In another process, over its pipes, with real Postgres and Redis:
tests/integration/test_phase29_observability_integration.py.
"""

import asyncio
import io
import json
import logging
import re
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry.instrumentation.utils import is_instrumentation_enabled
from opentelemetry.trace import SpanKind, StatusCode
from prometheus_client import REGISTRY

from app import main
from app.api.routes import trips
from app.api.routes.trips import _run_orchestrator, _start_run
from app.core.config import Settings, settings
from app.core.security import create_access_token
from app.db.session import get_db
from src.ai import llm, usage
from src.ai.mcp_client import client as mcp_client
from src.ai.mcp_client.client import call_tool
from src.ai.mcp_server import cache as tool_cache
from src.ai.mcp_server import outbound, server
from src.ai.mcp_server.config import MCPSettings
from src.ai.mcp_server.models import CACHES, TOOL_NAMES, ToolError
from src.ai.mcp_server.rate_limiter import StillLimited
from src.ai.orchestrator import orchestrator
from src.ai.orchestrator.orchestrator import _TokenStream, builder_failed_node, persist_node, retry_dispatch_node
from src.ai.utils.run_logger import timed_run
from src.telemetry import context, logs, traces
from tests.fakes import FakeChatModel, FakeProviders, models_answer, provider_answer, tool_server
from tests.unit.test_phase26_tracing import plan
from tests.unit.test_phase27_runs import Database, Redis
from tests.unit.test_trips import _make_trip, _make_user, _override_get_db

ROOT = Path(__file__).resolve().parents[2]
TRIP = "3f2a9c1d-5e6b-4a80-91a2-b3c4d5e6f708"
USER = "9c1d3f2a-6b5e-4a80-a291-c4b3e6d508f7"
DAY = date.today() + timedelta(days=30)
WEATHER = {"destination": "Goa", "date_range": f"{DAY} to {DAY + timedelta(days=2)}"}
FLIGHTS = {
    "origin": "DEL",
    "destination": "Goa",
    "date": str(DAY),
    "return_date": str(DAY + timedelta(days=3)),
    "budget": 50_000.0,
}
LARGE = "openai/gpt-oss-120b"


def metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def named(spans, name: str):
    found = [span for span in spans.get_finished_spans() if span.name == name]
    assert found, f"no span named {name!r} among {sorted({span.name for span in spans.get_finished_spans()})}"
    return found[-1]


def child_of(span, parent) -> bool:
    return span.parent is not None and span.parent.span_id == parent.context.span_id


# ── A request says whose it is ─────────────────────────────────────────────


def test_a_request_is_counted_inside_its_span_and_says_whose_it_is_inside_both():
    # Outermost first. The trace's own layer is put around all of these by the instrumentation.
    assert [layer.cls.__name__ for layer in main.app.user_middleware] == [
        "CountsRequests",
        "RequestContext",
        "NoStoreByDefault",
        "CORSMiddleware",
    ]
    assert main.app._is_instrumented_by_opentelemetry is True


async def heard_by(scope: dict) -> tuple[str | None, str | None]:
    """What a handler finds in the two context variables when this request reaches it."""
    heard: list = []

    async def handler(_scope, _receive, _send) -> None:
        heard.append((context.trip_id(), context.user_id()))

    await main.RequestContext(handler)({"type": "http", "headers": [], "query_string": b"", **scope}, None, None)
    assert (context.trip_id(), context.user_id()) == (None, None)  # and nothing of it is left afterwards
    return heard[0]


async def test_the_trip_is_read_from_the_address_and_the_user_from_the_login_token():
    token = create_access_token(USER)
    with_login = [(b"authorization", f"Bearer {token}".encode())]

    assert await heard_by({"path": f"/trips/{TRIP}/plan", "headers": with_login}) == (TRIP, USER)
    assert await heard_by({"path": f"/trips/{TRIP}", "headers": with_login}) == (TRIP, USER)
    assert await heard_by({"path": f"/trips/{TRIP.upper()}/status", "headers": with_login}) == (TRIP, USER)
    # an event stream: a browser cannot send the header, so the token is in the address
    assert await heard_by({"path": f"/trips/{TRIP}/stream", "query_string": f"token={token}".encode()}) == (TRIP, USER)
    # no trip in the address
    assert await heard_by({"path": "/trips", "headers": with_login}) == (None, USER)
    assert await heard_by({"path": "/trips/search", "headers": with_login}) == (None, USER)
    assert await heard_by({"path": f"/trips/{TRIP}0/plan", "headers": with_login}) == (None, USER)  # not an id
    assert await heard_by({"path": f"/admin/trips/{TRIP}", "headers": with_login}) == (None, USER)


async def test_a_token_that_does_not_hold_up_names_nobody_and_stops_nothing():
    path = {"path": f"/trips/{TRIP}/plan"}
    forged = create_access_token(USER)[:-4] + "AAAA"
    not_a_user = create_access_token("not-a-uuid")

    assert await heard_by(path) == (TRIP, None)
    assert await heard_by({**path, "headers": [(b"authorization", f"Bearer {forged}".encode())]}) == (TRIP, None)
    assert await heard_by({**path, "headers": [(b"authorization", f"Bearer {not_a_user}".encode())]}) == (TRIP, None)
    assert await heard_by({**path, "headers": [(b"authorization", b"Basic dXNlcjpwYXNz")]}) == (TRIP, None)
    assert await heard_by({**path, "query_string": b"token=&x=%zz"}) == (TRIP, None)

    passed_on = []

    async def lifespan(scope, _receive, _send) -> None:
        passed_on.append(scope["type"])

    await main.RequestContext(lifespan)({"type": "lifespan"}, None, None)  # not a request: handed on untouched
    assert passed_on == ["lifespan"]


async def test_a_requests_span_carries_the_trip_and_the_user_and_a_health_check_makes_none(spans):
    nobody_there = AsyncMock(get=AsyncMock(return_value=None))  # the token's user is not in the database
    main.app.dependency_overrides[get_db] = _override_get_db(nobody_there)
    try:
        async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as http:
            await http.get("/version")
            await http.get("/metrics")
            refused = await http.get(f"/trips/{TRIP}", headers={"Authorization": f"Bearer {create_access_token(USER)}"})
            anonymous = await http.get(f"/trips/{TRIP}")
    finally:
        main.app.dependency_overrides.pop(get_db, None)

    assert (refused.status_code, anonymous.status_code) == (401, 401)
    made = [span for span in spans.get_finished_spans() if span.kind is SpanKind.SERVER]
    assert [span.name for span in made] == ["GET /trips/{trip_id}"] * 2  # /version and /metrics start no trace
    claimed, nobody = made
    assert (claimed.attributes["trip.id"], claimed.attributes["user.id"]) == (TRIP, USER)  # whose it says it is
    assert nobody.attributes["trip.id"] == TRIP and "user.id" not in nobody.attributes
    assert claimed.attributes["http.route"] == "/trips/{trip_id}" and claimed.attributes["http.status_code"] == 401


async def test_a_trip_that_was_just_made_is_named_for_what_the_request_starts(no_cache_warming):
    """POST /trips has no trip in its address — there was none yet. The searches it starts are that trip's."""
    user_id = uuid.uuid4()
    made = _make_trip(user_id)
    database = AsyncMock(get=AsyncMock(return_value=_make_user(user_id)), add=MagicMock(), commit=AsyncMock())

    async def refresh(row) -> None:
        for field in (
            "id",
            "user_id",
            "destination",
            "start_date",
            "end_date",
            "budget",
            "group_size",
            "interests",
            "status",
        ):
            setattr(row, field, getattr(made, field))
        row.created_at = made.created_at

    database.refresh = refresh
    heard = []
    no_cache_warming.side_effect = lambda *_: heard.append((context.trip_id(), context.user_id()))
    main.app.dependency_overrides[get_db] = _override_get_db(database)
    try:
        with patch.object(settings, "CACHE_WARMING_ENABLED", True):
            async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as http:
                answer = await http.post(
                    "/trips",
                    headers={"Authorization": f"Bearer {create_access_token(str(user_id))}"},
                    json={
                        "destination": "Goa",
                        "start_date": str(DAY),
                        "end_date": str(DAY + timedelta(days=3)),
                        "budget": 50_000,
                        "group_size": 2,
                        "interests": ["beach"],
                    },
                )
    finally:
        main.app.dependency_overrides.pop(get_db, None)

    assert answer.status_code == 201
    assert heard == [(str(made.id), str(user_id))]


# ── GET /metrics, and who may read it ──────────────────────────────────────


async def metrics_answer(**headers: str) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as http:
        return await http.get("/metrics", headers=headers)


async def test_the_metrics_are_open_where_the_app_is_developed_and_closed_where_it_is_deployed():
    with patch.object(settings, "OPS_TOKEN", ""), patch.object(settings, "APP_ENV", "development"):
        open_ = await metrics_answer()
    with patch.object(settings, "OPS_TOKEN", ""), patch.object(settings, "APP_ENV", "production"):
        forgotten = await metrics_answer()  # a deployment that forgot the token does not publish its numbers

    assert open_.status_code == 200 and "itinerary_builds_total" in open_.text and "llm_calls_total" in open_.text
    assert open_.headers["cache-control"] == "no-store"
    assert forgotten.status_code == 404 and "itinerary_builds_total" not in forgotten.text
    assert forgotten.json()["detail"] == "Not Found"  # what a route that is not there says


async def test_with_an_operations_token_the_metrics_are_for_whoever_has_it():
    token = "an-operations-token-of-32-characters!"
    with patch.object(settings, "OPS_TOKEN", token), patch.object(settings, "APP_ENV", "production"):
        as_prometheus_sends_it = await metrics_answer(Authorization=f"Bearer {token}")
        as_the_other_operators_route = await metrics_answer(**{"X-Ops-Token": token})
        answers = {
            "none": await metrics_answer(),
            "wrong": await metrics_answer(Authorization="Bearer an-operations-token-of-32-characters?"),
            "a login": await metrics_answer(Authorization=f"Bearer {create_access_token(USER)}"),
            "not bearer": await metrics_answer(Authorization=token),
        }
    with patch.object(settings, "OPS_TOKEN", token), patch.object(settings, "APP_ENV", "development"):
        in_development_too = await metrics_answer()

    assert as_prometheus_sends_it.status_code == 200 and "http_requests_total" in as_prometheus_sends_it.text
    assert as_the_other_operators_route.status_code == 200
    assert {who: answer.status_code for who, answer in answers.items()} == dict.fromkeys(answers, 404)
    assert in_development_too.status_code == 404  # set is set, wherever


def test_a_deployment_is_told_what_it_cannot_be_seen_doing():
    """app.preflight (Phase 27) says what a deployment is missing before it starts. Phase 29's part of that."""
    from app import preflight
    from tests.unit.test_phase27_production import deployment, providers

    def said(**changes) -> tuple[bool, str]:
        report = preflight.check(deployment(**changes), providers())
        return report.ok, report.text()

    assert said() == (True, "Every setting a deployment needs is there.")
    # it runs without these — unseen
    ok, text = said(OTEL_EXPORTER_OTLP_ENDPOINT="")
    assert ok and "OTEL_EXPORTER_OTLP_ENDPOINT is empty: no request is traced" in text
    ok, text = said(OPS_TOKEN="")
    assert ok and "GET /metrics answers nobody, so Prometheus cannot read this deployment" in text
    assert "GET /admin/sentry-test is switched off" in text  # and the door it already opened
    ok, text = said(LOG_FORMAT="text")
    assert ok and "LOG_FORMAT is text: the log lines cannot be searched by trip_id" in text
    ok, text = said(OTEL_EXPORTER_OTLP_ENDPOINT="http://collector.internal:4317")
    assert ok and "ends in :4317, a collector's gRPC port" in text
    # it must not start on these: what was asked for cannot be done
    for endpoint in ("collector.internal:4318", "grpc://collector.internal:4317"):
        ok, text = said(OTEL_EXPORTER_OTLP_ENDPOINT=endpoint)
        assert not ok and "OTEL_EXPORTER_OTLP_ENDPOINT is not an http:// or https:// URL" in text, endpoint
    ok, text = said(OTEL_TRACES_SAMPLE_RATIO=1.5)
    assert not ok and "OTEL_TRACES_SAMPLE_RATIO is not between 0 and 1" in text


def test_a_tracing_backends_key_is_a_secret_like_any_other():
    from app.core.error_reporting import Scrubber, secrets_of
    from tests.unit.test_phase27_production import deployment

    key = "Authorization=Basic dXNlcjpzZWNyZXQta2V5LTAwMDE="
    secrets = secrets_of(deployment(OTEL_EXPORTER_OTLP_HEADERS=key))

    assert key in secrets
    assert key not in Scrubber(secrets).text(f"exporting with headers {key} failed")


def test_what_the_app_logs_and_traces_goes_through_the_scrubber_of_the_error_reports():
    """Phase 27's scrubber knows this deployment's secrets by value. The log lines and the spans are handed it."""
    source = (ROOT / "src/backend/app/main.py").read_text()

    assert main._scrub("https://api.openweathermap.org/forecast?q=Goa&appid=0123456789abcdef") == (
        "https://api.openweathermap.org/forecast?q=Goa&appid=[Filtered]"
    )
    assert "logs.configure(logs.chosen(settings.LOG_FORMAT, settings.APP_ENV), traces.BACKEND, scrub=_scrub)" in source
    assert "traces.configure(settings, traces.BACKEND, __version__, scrub=_scrub)" in source
    tool_server_source = (ROOT / "src/ai/mcp_server/server.py").read_text()  # and the other process's own
    assert "stream=sys.stderr, scrub=scrub" in tool_server_source
    assert "traces.configure(mcp_settings, traces.TOOL_SERVER, scrub=scrub)" in tool_server_source


def test_the_tool_server_is_given_the_same_settings_under_the_same_names():
    shared = (
        "APP_ENV",
        "LOG_FORMAT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_TRACES_SAMPLE_RATIO",
    )
    for name in shared:
        assert Settings.model_fields[name].default == MCPSettings.model_fields[name].default, name
    # off, and text, unless somebody says otherwise
    assert (
        Settings.model_fields["OTEL_EXPORTER_OTLP_ENDPOINT"].default == ""
        and Settings.model_fields["LOG_FORMAT"].default == ""
    )
    assert Settings.model_fields["OTEL_TRACES_SAMPLE_RATIO"].default == 1.0


# ── A planning run ─────────────────────────────────────────────────────────


def runs_ended(kind: str) -> dict[str, float]:
    outcomes = ("completed", "no_plan", "crashed", "interrupted")
    return {outcome: metric("planning_run_duration_seconds_count", kind=kind, outcome=outcome) for outcome in outcomes}


def since(before: dict[str, float], kind: str) -> dict[str, float]:
    return {outcome: count - before[outcome] for outcome, count in runs_ended(kind).items() if count != before[outcome]}


async def test_a_run_is_one_span_under_the_request_that_started_it_and_is_counted_by_how_it_ended(spans):
    trip_id, redis, before = uuid.uuid4(), Redis(), runs_ended("refine")
    heard = {}

    async def a_plan(_agent, _db, _hooks) -> dict:
        heard.update(trip=context.trip_id(), in_progress=metric("planning_runs_in_progress"))
        with traces.span("orchestrator"):
            return {"itinerary_id": uuid.uuid4()}

    running = metric("planning_runs_in_progress")
    with patch.object(trips, "AsyncSessionLocal", Database()), traces.span("POST /trips/{trip_id}/refine") as request:
        started = asyncio.create_task(_run_orchestrator(trip_id, redis, a_plan, turn=3, kind="refine"))
    await started  # the request is over; the run it started goes on, in the same trace

    run = named(spans, "planning run")
    assert run.parent.span_id == request.get_span_context().span_id
    assert dict(run.attributes) == {"run.kind": "refine", "run.turn": 3, "trip.id": str(trip_id)}
    assert child_of(named(spans, "orchestrator"), run) and run.status.status_code is StatusCode.UNSET
    assert heard == {"trip": str(trip_id), "in_progress": running + 1}  # the run knows whose it is, whoever started it
    assert since(before, "refine") == {"completed": 1} and metric("planning_runs_in_progress") == running


async def test_a_run_that_ends_without_a_plan_or_crashes_or_is_cut_short_is_counted_as_that(spans):
    before, redis = runs_ended("plan"), Redis()

    async def nothing_found(_agent, _db, _hooks) -> dict:
        return {"itinerary_id": None}  # the graph has said why, and marked the trip

    async def falls_over(_agent, _db, _hooks) -> dict:
        raise RuntimeError("the builder fell over")

    async def never_finishes(_agent, _db, _hooks) -> dict:
        await asyncio.sleep(3600)
        return {}

    with patch.object(trips, "AsyncSessionLocal", Database()):
        await _run_orchestrator(uuid.uuid4(), redis, nothing_found)
        assert since(before, "plan") == {"no_plan": 1}

        await _run_orchestrator(uuid.uuid4(), redis, falls_over)  # dealt with inside: nothing is raised
        assert since(before, "plan") == {"no_plan": 1, "crashed": 1}
        crashed = named(spans, "planning run")
        assert crashed.status.status_code is StatusCode.ERROR and "the builder fell over" in crashed.status.description
        assert crashed.events[0].attributes["exception.type"] == "RuntimeError"

        stopping = asyncio.create_task(_run_orchestrator(uuid.uuid4(), redis, never_finishes))
        await asyncio.sleep(0.02)
        stopping.cancel()  # the server is stopping
        with pytest.raises(asyncio.CancelledError):
            await stopping
    assert since(before, "plan") == {"no_plan": 1, "crashed": 1, "interrupted": 1}


async def test_what_a_run_is_counted_as_is_what_started_it():
    started_as = []

    def a_run(*_args, **how):
        started_as.append(how["kind"])
        return MagicMock()

    trip = MagicMock(id=uuid.uuid4(), status="pending")
    with patch.object(trips, "_run_orchestrator", a_run), patch.object(trips, "spawn"):
        for what in (
            {},  # POST /plan, POST /clarify
            {"choice": "cheaper_flights"},  # POST /replan
            {"turn": 2, "refinement_type": "targeted_hotel"},  # POST /refine
            {"turn": 2, "refinement_type": "targeted_flights", "retry": "flight_agent"},  # POST /retry
        ):
            await _start_run(trip, AsyncMock(add=MagicMock()), Redis(), AsyncMock(), **what)

    assert started_as == ["plan", "replan", "refine", "retry"]


# ── The planner: every agent a span, every model call counted for its agent ─


async def test_work_that_has_a_name_is_a_span_of_that_name(spans):
    async with timed_run("flight_agent") as timer:
        with traces.span("mcp.call search_flights"):
            pass
    async with timed_run():  # timed and metered, and nobody's: no span
        pass

    assert [span.name for span in spans.get_finished_spans()] == ["mcp.call search_flights", "flight_agent"]
    agent = named(spans, "flight_agent")
    assert dict(agent.attributes) == {"agent.name": "flight_agent"} and child_of(
        named(spans, "mcp.call search_flights"), agent
    )
    assert timer.usage.agent == "flight_agent"


def calls(agent: str, model: str) -> tuple[float, ...]:
    labels = {"agent": agent, "model": model}
    return (
        metric("llm_calls_total", status="ok", **labels),
        metric("llm_calls_total", status="error", **labels),
        metric("llm_tokens_total", direction="input", **labels),
        metric("llm_tokens_total", direction="output", **labels),
        metric("llm_cost_usd_total", **labels),
    )


def more(before: tuple[float, ...], agent: str, model: str) -> list[float]:
    return [round(now - then, 8) for now, then in zip(calls(agent, model), before)]


async def test_a_model_call_is_counted_for_the_agent_that_asked_and_is_a_span_under_its_work(spans):
    before, nobodys = calls("itinerary_builder", LARGE), calls("unmetered", LARGE)
    with models_answer(large="a plan", tokens=(2_000, 2_400)):
        import langchain_groq  # inside: models_answer() puts a stand-in behind the name, for as long as it is open

        async with timed_run("itinerary_builder") as timer:
            await langchain_groq.ChatGroq(model=LARGE).ainvoke("Plan three days in Goa")
            pieces = [piece async for piece in langchain_groq.ChatGroq(model=LARGE).astream("…and again, streamed")]
        await langchain_groq.ChatGroq(model=LARGE).ainvoke("asked by nobody's work")

    one_call_costs = (2_000 * 0.15 + 2_400 * 0.60) / 1_000_000  # list price: src/ai/usage.py
    assert more(before, "itinerary_builder", LARGE) == [2, 0, 4_000, 4_800, round(2 * one_call_costs, 8)]
    assert len(pieces) > 1  # streamed in pieces, counted once
    assert more(nobodys, "unmetered", LARGE)[:2] == [1, 0]
    assert timer.usage.token_count == 8_800  # the meter of Phase 26 is credited as before

    work = named(spans, "itinerary_builder")
    asked = [span for span in spans.get_finished_spans() if span.name == f"llm {LARGE}"]
    assert len(asked) == 3 and [child_of(span, work) for span in asked] == [True, True, False]
    assert asked[0].kind is SpanKind.CLIENT
    assert dict(asked[0].attributes) == {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": LARGE,
        "gen_ai.response.model": LARGE,
        "gen_ai.usage.input_tokens": 2_000,
        "gen_ai.usage.output_tokens": 2_400,
        "agent.name": "itinerary_builder",
    }


async def test_a_model_is_counted_under_the_name_it_was_asked_for_not_the_one_it_answered_with(spans):
    """A provider answers under a dated name of its own. A label that followed it would split one model's
    calls in two — and its failures, which have no answer to read a name from, into a third."""

    class Dated(FakeChatModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            answer = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
            answer.generations[0].message.response_metadata["model_name"] = f"{self.model}-20260801"
            return answer

    before, dated = calls("evaluator", LARGE), calls("evaluator", f"{LARGE}-20260801")
    async with timed_run("evaluator") as timer:
        await Dated(model=LARGE, answer="fine", tokens=(100, 20)).ainvoke("Is this plan sound?")

    assert more(before, "evaluator", LARGE)[:4] == [1, 0, 100, 20]
    assert more(dated, "evaluator", f"{LARGE}-20260801") == [0, 0, 0, 0, 0]
    asked = named(spans, f"llm {LARGE}")
    assert asked.attributes["gen_ai.request.model"] == LARGE
    assert asked.attributes["gen_ai.response.model"] == f"{LARGE}-20260801"  # the span says both
    assert timer.usage.by_model == {f"{LARGE}-20260801": [100, 20]}  # and the meter prices what answered, as before


async def test_a_model_call_that_fails_is_counted_as_an_error_and_its_span_says_why(spans):
    before = calls("destination_intelligence", LARGE)
    with models_answer(large=RuntimeError("429: rate limit reached for model")):
        import langchain_groq

        async with timed_run("destination_intelligence") as timer:
            with pytest.raises(RuntimeError):
                await langchain_groq.ChatGroq(model=LARGE).ainvoke("What should a visitor to Jaipur know?")

    assert more(before, "destination_intelligence", LARGE) == [0, 1, 0, 0, 0]
    assert timer.usage.token_count is None  # no answer, no tokens: nothing is made up
    failed = named(spans, f"llm {LARGE}")
    assert failed.status.status_code is StatusCode.ERROR and "rate limit reached" in failed.status.description
    assert failed.events[0].attributes["exception.type"] == "RuntimeError"
    assert usage._recorder.get()._under_way == {}  # nothing is kept waiting for an end that came


def calls_made() -> dict[tuple[str, str, str], float]:
    """llm_calls_total as it stands: {(agent, model, status): count}."""
    return {
        (sample.labels["agent"], sample.labels["model"], sample.labels["status"]): sample.value
        for family in REGISTRY.collect()
        if family.name == "llm_calls"
        for sample in family.samples
        if sample.name == "llm_calls_total"
    }


async def test_a_whole_plan_is_spans_from_the_run_to_the_models_and_every_asker_is_on_the_list(spans):
    before_builds = metric("itinerary_builds_total", status="success")
    before_calls = calls_made()

    with traces.span("planning run") as run:
        _trip_id, result = await plan()

    assert result["draft_itinerary"] is not None
    assert metric("itinerary_builds_total", status="success") - before_builds == 1
    whole = named(spans, "orchestrator")
    assert whole.parent.span_id == run.get_span_context().span_id
    made = spans.get_finished_spans()
    for agent in (
        "intent_parsing",
        "flight_agent",
        "budget_decision",
        "hotel_agent",
        "activities_agent",
        "destination_intelligence",
        "itinerary_builder",
        "evaluator",
    ):
        assert named(spans, agent).context.trace_id == whole.context.trace_id, agent
    # each model call under the work it was part of: the request read, the tips written, the plan built
    asked_under = sorted(
        (span.name, parent.name)
        for span in made
        if span.name.startswith("llm ")
        for parent in made
        if child_of(span, parent)
    )
    # (the models as this environment names them: .env may say another than the code's default)
    assert asked_under == sorted(
        [
            (f"llm {llm.GEMINI_MODEL}", "intent_parsing"),
            (f"llm {llm.GROQ_MODEL}", "destination_intelligence"),
            (f"llm {llm.GROQ_MODEL}", "itinerary_builder"),
        ]
    )
    parents = {span.context.span_id for span in spans.get_finished_spans()} | {run.get_span_context().span_id}
    assert all(
        span.parent is None or span.parent.span_id in parents for span in spans.get_finished_spans()
    )  # no orphans

    asked = {who for who, count in calls_made().items() if count > before_calls.get(who, 0)}
    assert asked, "the plan asked no model at all"
    # whoever asks a model is on the list that makes its counters exist before the first call
    assert {agent for agent, _model, _status in asked} <= set(llm.ASKED_BY), asked


def test_every_agent_that_asks_a_model_has_its_counters_from_the_start():
    for agent, models in llm.ASKED_BY.items():
        assert models, agent
        for model in models:
            for status in ("ok", "error"):
                value = REGISTRY.get_sample_value("llm_calls_total", {"agent": agent, "model": model, "status": status})
                assert value is not None, (agent, model, status)
    assert set(llm.ASKED_BY["itinerary_builder"]) == {llm.GROQ_MODEL}
    assert set(llm.ASKED_BY["intent_parsing"]) == {llm.GEMINI_MODEL, llm.GROQ_SMALL_MODEL}  # and its stand-in


async def test_a_build_is_counted_by_its_fate():
    def builds() -> dict[str, float]:
        return {status: metric("itinerary_builds_total", status=status) for status in ("success", "failure", "retry")}

    def since_then(before: dict[str, float]) -> dict[str, float]:
        return {status: count - before[status] for status, count in builds().items() if count != before[status]}

    before = builds()
    sent_back = {
        "evaluator_verdict": {
            "passed": False,
            "failures": [{"check": "hallucinated_activity", "detail": "x"}],
            "retry_count": 0,
        },
        "evaluator_retry_count": 0,
    }
    with patch.object(orchestrator, "ActivitiesAgent") as agent:
        agent.return_value.run = AsyncMock(return_value={"attractions": [], "error": None})
        await retry_dispatch_node(sent_back)
    assert since_then(before) == {"retry": 1}  # sent back by the evaluator: built again

    await persist_node({"draft_itinerary": {"days": [], "total_cost": 0}})  # passed; with nowhere to save it
    assert since_then(before) == {"retry": 1, "success": 1}

    database = AsyncMock(get=AsyncMock(return_value=MagicMock()), add=MagicMock())
    with patch.object(orchestrator, "spawn"), patch.object(orchestrator, "generate_embeddings", MagicMock()):
        await persist_node({"db": database, "trip_id": uuid.uuid4(), "draft_itinerary": {"days": [], "total_cost": 0}})
    assert since_then(before) == {"retry": 1, "success": 2}

    database.commit = AsyncMock(side_effect=RuntimeError("the database is away"))
    with pytest.raises(RuntimeError):
        await persist_node({"db": database, "trip_id": uuid.uuid4(), "draft_itinerary": {"days": []}})
    assert since_then(before) == {"retry": 1, "success": 2}  # not saved: not a success

    await builder_failed_node(
        {"builder_error": {"error": "LLM timeout", "code": "LLM_ERROR"}, "evaluator_retry_count": 3}
    )
    assert since_then(before) == {"retry": 1, "success": 2, "failure": 1}


async def test_the_pieces_of_an_itinerary_are_published_and_not_recorded_one_by_one():
    recorded = []

    async def publish(event: dict) -> None:
        recorded.append((event["event"], is_instrumentation_enabled()))

    state = {"publish_fn": publish}
    stream = _TokenStream(state)
    await stream.push('{"days": [')
    await stream.flush()
    await orchestrator._publish(state, {"event": "planning_complete"})

    # twenty of the first a second, each a Redis command that says nothing; one of the second, which does
    assert recorded == [("builder_token", False), ("planning_complete", True)]


# ── The tool server: another process, the same trace ───────────────────────


class Kept:
    """The tool server's Redis, as a dict: what the real get_cached_sync / set_cached_sync talk to."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value


def lookups() -> dict[tuple[str, str], float]:
    return {
        (cache, result): metric("mcp_cache_lookups_total", cache=cache, result=result)
        for cache in CACHES
        for result in ("hit", "miss", "error")
    }


def looked_up(before: dict) -> dict[tuple[str, str], float]:
    return {key: count - before[key] for key, count in lookups().items() if count != before[key]}


async def test_a_call_to_the_tool_server_takes_the_trace_and_the_trip_with_it(spans):
    providers, kept = FakeProviders(), Kept()
    before = metric("mcp_tool_calls_total", tool="search_flights", status="ok")
    timed = metric("mcp_tool_call_duration_seconds_count", tool="search_flights")

    with patch.object(tool_cache, "_get_client", lambda: kept):
        async with tool_server(providers):
            with context.scope(trip_id=TRIP, user_id=USER), traces.span("flight_agent") as agent:
                flights = await call_tool("search_flights", FLIGHTS)

    assert len(flights) == 2 and providers.counts()["duffel"] == 1
    sent, answered, asked = (
        named(spans, name) for name in ("mcp.call search_flights", "tools/call search_flights", "provider duffel")
    )
    # one trace: the agent → the call it sent → the call the server answered → the request to the provider
    assert sent.parent.span_id == agent.get_span_context().span_id and sent.kind is SpanKind.CLIENT
    assert child_of(answered, sent) and answered.kind is SpanKind.SERVER and answered.parent.is_remote
    assert child_of(asked, answered) and asked.kind is SpanKind.CLIENT
    assert {span.context.trace_id for span in (sent, answered, asked)} == {agent.get_span_context().trace_id}
    # …and whose it is went with it: the server's spans say so themselves
    for span in (sent, answered, asked):
        assert (span.attributes["trip.id"], span.attributes["user.id"]) == (TRIP, USER), span.name
    assert sent.attributes["mcp.tool.name"] == answered.attributes["mcp.tool.name"] == "search_flights"
    assert dict(asked.attributes) == {
        "peer.service": "duffel",
        "http.response.status_code": 200,
        "provider.attempts": 1,
        "trip.id": TRIP,
        "user.id": USER,
    }  # which provider and how it went — never the address: some take their key in it
    assert metric("mcp_tool_calls_total", tool="search_flights", status="ok") - before == 1
    assert metric("mcp_tool_call_duration_seconds_count", tool="search_flights") - timed == 1


async def test_what_the_cache_did_for_a_call_comes_back_with_the_answer_and_is_counted(spans):
    providers, kept, before = FakeProviders(), Kept(), lookups()

    with patch.object(tool_cache, "_get_client", lambda: kept):
        async with tool_server(providers):
            first = await call_tool("search_flights", FLIGHTS)  # nothing cached: the provider is asked
            assert looked_up(before) == {("flights", "miss"): 1}
            again = await call_tool("search_flights", FLIGHTS)  # the same search: answered from the cache
            assert looked_up(before) == {("flights", "miss"): 1, ("flights", "hit"): 1}

    assert first == again and providers.counts()["duffel"] == 1
    assert any(key.startswith("mcp:flights:") for key in kept.values)

    with patch.object(tool_cache, "_get_client", lambda: None):  # Redis is not there
        async with tool_server(providers):
            await call_tool("search_flights", FLIGHTS)
    # looked up and not found, and not a miss either: a ratio of hits must fall when the cache is gone
    assert looked_up(before) == {("flights", "miss"): 1, ("flights", "hit"): 1, ("flights", "error"): 1}


async def test_a_line_logged_inside_the_tool_server_carries_the_callers_trip(spans):
    providers, kept = FakeProviders(), Kept()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.JsonLines())
    handler.addFilter(logs.ContextFilter(traces.TOOL_SERVER))
    cache_logger = logging.getLogger("src.ai.mcp_server.cache")
    before = cache_logger.level
    cache_logger.addHandler(handler)
    cache_logger.setLevel(logging.INFO)
    try:
        with patch.object(tool_cache, "_get_client", lambda: kept):
            async with tool_server(providers):
                with context.scope(trip_id=TRIP, user_id=USER), traces.span("flight_agent") as agent:
                    await call_tool("search_flights", FLIGHTS)
                await call_tool("search_flights", FLIGHTS)  # the MCP Inspector, say: a caller that sends nothing
    finally:
        cache_logger.removeHandler(handler)
        cache_logger.setLevel(before)

    miss, hit = [json.loads(line) for line in stream.getvalue().splitlines()]
    # written by a worker thread of the server, while it answered a call from somebody's request
    assert miss["message"].startswith("[CACHE MISS] mcp:flights:") and miss["service"] == "tripplanner-mcp"
    assert (miss["trip_id"], miss["user_id"]) == (TRIP, USER)
    assert miss["trace_id"] == format(agent.get_span_context().trace_id, "032x")
    assert hit["message"].startswith("[CACHE HIT]") and (hit["trip_id"], hit["user_id"]) == (None, None)
    assert hit["trace_id"] not in (None, miss["trace_id"])  # a trace of its own, about nobody's trip


async def test_a_tool_that_answers_with_an_error_is_a_failed_call_on_both_sides(spans):
    before = metric("mcp_tool_calls_total", tool="get_weather", status="error")

    async with tool_server(FakeProviders()):
        with traces.span("activities_agent"):
            result = await call_tool("get_weather", {"destination": "Goa", "date_range": "sometime"})

    assert isinstance(result, ToolError)
    sent, answered = named(spans, "mcp.call get_weather"), named(spans, "tools/call get_weather")
    assert sent.status.status_code is StatusCode.ERROR and sent.attributes["error.type"] == result.code
    assert answered.status.status_code is StatusCode.ERROR and result.code in answered.status.description
    assert metric("mcp_tool_calls_total", tool="get_weather", status="error") - before == 1


async def test_a_call_that_belongs_to_no_request_is_sent_as_it_always_was():
    session = AsyncMock()
    session.call_tool.return_value = MagicMock(isError=False, structuredContent={"result": []}, content=[])

    with patch.object(mcp_client, "get_session", AsyncMock(return_value=session)):
        await call_tool("search_flights", FLIGHTS)  # no trace under way, nobody's trip: nothing to send beside it
        with context.scope(trip_id=TRIP):
            await call_tool("search_flights", FLIGHTS)

    plain, with_the_trip = session.call_tool.call_args_list
    assert plain.kwargs == {"arguments": {"input": FLIGHTS}}
    assert with_the_trip.kwargs == {"arguments": {"input": FLIGHTS}, "meta": {"trip_id": TRIP}}


async def test_the_session_is_made_in_nobodys_context_whichever_request_needed_it_first(spans):
    """Its tasks live as long as the process. Made inside the first request, they would log under that
    trip — and as part of that request's trace — for every request after it."""
    made_in = {}

    class Pipes:
        async def __aenter__(self):
            made_in["pipes"] = (context.trip_id(), traces.current_ids()[0])
            return "read", "write"

        async def __aexit__(self, *_exc):
            return False

    class Session:
        def __init__(self, _read, _write, logging_callback=None) -> None:
            made_in["session"] = (context.trip_id(), traces.current_ids()[0])
            made_in["hears"] = logging_callback

        async def __aenter__(self):
            return self

        async def initialize(self) -> None:
            made_in["hello"] = (context.trip_id(), traces.current_ids()[0])

    with (
        patch.object(mcp_client, "_session", None),
        patch.object(mcp_client, "_client_context", None),
        patch.object(mcp_client, "stdio_client", lambda _params, errlog=None: made_in.update(errlog=errlog) or Pipes()),
        patch.object(mcp_client, "ClientSession", Session),
    ):
        with context.scope(trip_id=TRIP, user_id=USER), traces.span("the first request to search"):
            session = await mcp_client.get_session()
            assert context.trip_id() == TRIP and traces.current_ids()[0] is not None  # the request itself is as it was

    assert isinstance(session, Session)
    assert made_in["pipes"] == made_in["session"] == made_in["hello"] == (None, None)
    assert made_in["hears"] is mcp_client._on_server_log  # and it listens for the cache's reports
    assert made_in["errlog"] is sys.stderr  # the server's log lines: to this process's standard error, as it is now


async def test_what_else_the_server_says_is_not_mistaken_for_a_cache_report():
    before = lookups()
    say = mcp_client._on_server_log

    await say(MagicMock(logger="mcp.server", data={"lookups": {"flights": {"hit": 5}}}))  # somebody else's message
    await say(MagicMock(logger="tripplanner.cache", data="3 hits"))
    await say(MagicMock(logger="tripplanner.cache", data={"lookups": {"flights": {"hit": "many"}, "hotels": 4}}))
    assert looked_up(before) == {}

    await say(MagicMock(logger="tripplanner.cache", data={"lookups": {"hotels": {"hit": 2, "miss": 1}}}))
    assert looked_up(before) == {("hotels", "hit"): 2, ("hotels", "miss"): 1}


def test_the_callers_lists_are_the_servers_own_tools_and_caches():
    assert tuple(tool.__name__ for tool in server.TOOLS) == TOOL_NAMES
    keys = re.findall(r'make_cache_key\("([a-z_]+)', (ROOT / "src/ai/mcp_server/tools.py").read_text())
    assert sorted(set(keys)) == sorted(CACHES)  # every cache the tools keep has its counters from the start
    for tool in TOOL_NAMES:
        assert REGISTRY.get_sample_value("mcp_tool_calls_total", {"tool": tool, "status": "error"}) is not None
    tally = tool_cache.Tally()
    for key in ("mcp:geocode:v2:9f8e", "mcp:attractions:v3:1a2b", "mcp:flights:ad75", "something else"):
        tally.add(key, "hit")
    assert tally.counted() == {
        "geocode": {"hit": 1},
        "attractions": {"hit": 1},
        "flights": {"hit": 1},
        "other": {"hit": 1},
    }


# ── A request to a provider ────────────────────────────────────────────────


def test_a_request_to_a_provider_is_a_span_that_says_how_it_went_and_never_where(spans, no_waiting):
    address = "https://api.openweathermap.org/data/2.5/forecast?q=Goa&appid=a-providers-key"
    answers = iter([provider_answer(503, url=address), provider_answer(200, {"list": []}, url=address)])

    outbound.send("openweather", lambda: next(answers))  # turned away once, then answered

    twice = named(spans, "provider openweather")
    assert dict(twice.attributes) == {
        "peer.service": "openweather",
        "http.response.status_code": 200,
        "provider.attempts": 2,
    }
    assert twice.status.status_code is StatusCode.UNSET

    with pytest.raises(StillLimited):
        outbound.send("openweather", lambda: provider_answer(429, url=address))  # every attempt turned away

    refused = named(spans, "provider openweather")
    assert refused.attributes["provider.attempts"] == outbound.ATTEMPTS == 4
    assert refused.attributes["http.response.status_code"] == 429 and refused.status.status_code is StatusCode.ERROR
    assert "appid" not in json.dumps(dict(twice.attributes))  # the address is nowhere in what the span was given

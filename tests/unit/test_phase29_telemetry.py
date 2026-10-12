"""Phase 29 — src/telemetry: whose the work is, the log lines, the traces, the metrics.

The package on its own: two context variables, a log line as JSON with the trip, the user and the
trace on it, spans that leave scrubbed, and the app's own counters. How the app uses it is
tests/unit/test_phase29_wiring.py; the stack that reads it, tests/unit/test_phase29_stack.py; real
Postgres, Redis and a tool server in another process, tests/integration/test_phase29_observability_integration.py.
"""

import asyncio
import io
import json
import logging
import math
import re
import warnings
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace
from opentelemetry.instrumentation.utils import is_instrumentation_enabled
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy import create_engine, text

from app.core.error_reporting import Scrubber
from src.telemetry import context, logs, metrics, traces

TRIP = "3f2a9c1d-5e6b-4a80-91a2-b3c4d5e6f708"
USER = "9c1d3f2a-6b5e-4a80-a291-c4b3e6d508f7"
A_PROVIDERS_KEY = "sk_live_9f8e7d6c5b4a39281706"
A_LOGIN_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI5YzFkM2YyYSJ9.c2lnbmF0dXJlLXNpZ25hdHVyZQ"


def metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def named(spans, name: str):
    found = [span for span in spans.get_finished_spans() if span.name == name]
    assert found, f"no span named {name!r} among {[span.name for span in spans.get_finished_spans()]}"
    return found[-1]


# ── Whose the work is ──────────────────────────────────────────────────────


async def test_what_a_request_is_about_goes_with_every_task_it_starts_and_nowhere_else():
    seen: dict = {}

    async def the_run() -> None:
        await asyncio.sleep(0.01)  # still at work after the request that started it is over
        seen.update(trip=context.trip_id(), user=context.user_id(), attributes=context.attributes())

    assert (context.trip_id(), context.user_id()) == (None, None) and context.attributes() == {}
    with context.scope(trip_id=TRIP, user_id=USER):
        started = asyncio.create_task(the_run())
        assert context.attributes() == {"trip.id": TRIP, "user.id": USER}
    assert (context.trip_id(), context.user_id()) == (None, None)  # the request is over: nothing left behind

    await started
    assert seen == {"trip": TRIP, "user": USER, "attributes": {"trip.id": TRIP, "user.id": USER}}


async def test_binding_changes_only_what_it_is_given_and_tells_the_span_under_way(spans):
    async def a_request() -> None:
        with traces.span("the request"):  # begun before anyone knew which trip
            context.bind(user_id=USER)
            context.bind(trip_id=TRIP)  # the user stays
            assert (context.trip_id(), context.user_id()) == (TRIP, USER)

    await asyncio.create_task(a_request())  # a task of its own, as every request is

    assert (context.trip_id(), context.user_id()) == (None, None)
    assert dict(named(spans, "the request").attributes) == {"trip.id": TRIP, "user.id": USER}


def test_a_scope_inside_a_scope_gives_way_to_the_outer_one_again():
    with context.scope(trip_id=TRIP, user_id=USER):
        with context.scope():  # nobody's: what a connection kept for the life of the process is made in
            assert context.attributes() == {}
        assert context.attributes() == {"trip.id": TRIP, "user.id": USER}


# ── Log lines ──────────────────────────────────────────────────────────────


@pytest.fixture
def written():
    """A logger whose lines are written as the app's are in JSON. → (logger, lines() -> [dict])"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.JsonLines(Scrubber([A_PROVIDERS_KEY]).text))
    handler.addFilter(logs.ContextFilter("tripplanner-backend"))
    logger = logging.getLogger("src.phase29.test")
    logger.handlers, logger.propagate, logger.level = [handler], False, logging.INFO

    def lines() -> list[dict]:
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    yield logger, lines
    logger.handlers = []


def test_a_log_line_is_one_json_object_that_says_whose_it_is(written, spans):
    logger, lines = written

    logger.info("[STARTUP] ready")  # nobody's
    with context.scope(trip_id=TRIP, user_id=USER), traces.span("a request") as span:
        logger.warning("[CACHE WARM] flights: DEL → Goa — %d result(s) ready, ₹%s", 3, "42,000")
        trace_id, span_id = traces.current_ids()
        assert trace_id == format(span.get_span_context().trace_id, "032x")

    startup, warm = lines()
    assert list(startup)[:4] == ["timestamp", "level", "logger", "message"]
    assert startup == {
        "timestamp": startup["timestamp"],
        "level": "info",
        "logger": "src.phase29.test",
        "message": "[STARTUP] ready",
        "service": "tripplanner-backend",
        "trip_id": None,  # there, and empty: a field that is sometimes missing is harder to search
        "user_id": None,
        "trace_id": None,
        "span_id": None,
    }
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00", startup["timestamp"]
    )  # UTC, to the millisecond
    assert warm["level"] == "warning" and warm["message"].endswith("3 result(s) ready, ₹42,000")
    assert (warm["trip_id"], warm["user_id"]) == (TRIP, USER)
    assert (warm["trace_id"], warm["span_id"]) == (trace_id, span_id) and len(trace_id) == 32 and len(span_id) == 16


def test_a_line_is_written_with_its_own_letters_and_whatever_the_caller_added(written):
    logger, lines = written
    stream = logger.handlers[0].stream

    logger.info(
        "Jaipur — ₹70,000", extra={"provider": "duffel", "attempt": 2, "color_message": "\x1b[32mJaipur\x1b[0m"}
    )

    assert "Jaipur — ₹70,000" in stream.getvalue()  # not \u2014 and \u20b9: a line is read by people too
    (line,) = lines()
    assert (line["provider"], line["attempt"]) == ("duffel", 2)
    assert "color_message" not in line  # the web server's own copy of a line, with colour codes


def test_a_secret_in_a_message_or_a_traceback_is_not_written(written):
    logger, lines = written
    address = f"https://api.openweathermap.org/data/2.5/forecast?q=Goa&appid={A_PROVIDERS_KEY}"

    logger.error("Weather request failed: %s", address)
    try:
        raise RuntimeError(f"Client error '401 Unauthorized' for url '{address}'")
    except RuntimeError:
        logger.exception("Unexpected error in get_weather")

    message, crash = lines()
    assert A_PROVIDERS_KEY not in json.dumps([message, crash])
    assert "appid=[Filtered]" in message["message"]
    assert "RuntimeError" in crash["exc_info"] and "[Filtered]" in crash["exc_info"]  # the traceback is kept, cleaned


def test_an_access_line_has_its_parts_as_fields_and_not_the_address_it_came_from():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.JsonLines())
    handler.addFilter(logs.ContextFilter("tripplanner-backend"))
    access = logging.getLogger("uvicorn.access")
    before = (access.handlers, access.propagate, access.level, list(access.filters))
    access.handlers, access.propagate, access.level = [handler], False, logging.INFO
    access.filters = [logs.QuietProbes()]
    how = '%s - "%s %s HTTP/%s" %d'  # the web server's own format, and its five arguments
    try:
        with context.scope(trip_id=TRIP, user_id=USER):
            access.info(how, "203.0.113.7:51234", "GET", f"/trips/{TRIP}/stream?token=[Filtered]", "1.1", 200)
        access.info(how, "10.0.0.1:40000", "GET", "/ready", "1.1", 200)  # a health check that was answered
        access.info(how, "10.0.0.1:40000", "GET", "/metrics", "1.1", 200)
        access.info(how, "10.0.0.1:40000", "GET", "/ready", "1.1", 503)  # one that was not
    finally:
        access.handlers, access.propagate, access.level, access.filters = before

    stream_line, not_ready = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert stream_line["message"] == f'"GET /trips/{TRIP}/stream?token=[Filtered] HTTP/1.1" 200'
    assert "203.0.113.7" not in json.dumps(stream_line)  # whose it was is user_id
    assert (stream_line["method"], stream_line["path"], stream_line["status"]) == ("GET", f"/trips/{TRIP}/stream", 200)
    assert (stream_line["trip_id"], stream_line["user_id"]) == (TRIP, USER)
    assert (not_ready["path"], not_ready["status"]) == ("/ready", 503)  # a failed check is still a line


def test_the_format_is_what_the_setting_says_else_json_only_in_production():
    assert logs.chosen("json", "development") == "json" and logs.chosen("TEXT ", "production") == "text"
    assert logs.chosen("", "production") == "json" and logs.chosen("", "Production") == "json"
    assert logs.chosen("", "development") == "text" and logs.chosen("", "test") == "text"
    assert logs.chosen("yaml", "development") == "text"  # not a format: the default for where it runs


@pytest.fixture
def logging_as_it_was():
    names = ("", "py.warnings", *logs.TAKEN_OVER, *logs.INFO_FROM)
    kept = {
        name: (list(logger.handlers), logger.level, logger.propagate, list(logger.filters))
        for name in names
        for logger in [logging.getLogger(name)]
    }
    yield
    logging.captureWarnings(False)
    for name, (handlers, level, propagate, filters) in kept.items():
        logger = logging.getLogger(name)
        logger.handlers, logger.level, logger.propagate, logger.filters = handlers, level, propagate, filters


def test_in_json_every_line_of_the_process_is_one_object_whoever_logged_it(logging_as_it_was):
    stream = io.StringIO()
    theirs = logging.StreamHandler(io.StringIO())
    logging.getLogger("uvicorn.error").handlers = [theirs]  # as the web server sets itself up
    echoing = create_engine("sqlite://", echo=True)  # as the app's engine is made where APP_ENV is development
    logging.getLogger("uvicorn.error").propagate = False

    assert logs.configure("text", "tripplanner-backend", stream) is False  # the console stays as it was
    assert logging.getLogger("uvicorn.error").handlers == [theirs]

    assert logs.configure("json", "tripplanner-backend", stream) is True
    assert logs.configure("json", "tripplanner-backend", stream) is True  # again: replaced, not added to
    root = logging.getLogger()
    assert len(root.handlers) == 1 and isinstance(root.handlers[0].formatter, logs.JsonLines)
    for name in logs.TAKEN_OVER:
        assert logging.getLogger(name).handlers == [] and logging.getLogger(name).propagate is True, name

    with echoing.connect() as connection:  # its echo of every statement: a span where lines are stored, not a line
        assert connection.execute(text("SELECT 41 + 1")).scalar() == 42
    logging.getLogger("sqlalchemy.engine.Engine").warning("a warning of the engine's is still heard")
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 1)
    logging.getLogger("app.main").info("[STARTUP] production")
    logging.getLogger("src.ai.mcp_server.cache").info("[CACHE HIT]  mcp:flights:ad75")
    library = logging.getLogger("some.http.library")  # a library, at INFO: every address it calls, keys and all
    library.info("HTTP Request: GET https://api.example.com/?key=secret")
    library.warning("a library's warning is heard")
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("Model 'gemini-3.5-flash' is about to be renamed", UserWarning, stacklevel=1)

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]  # every line parses
    assert [(line["logger"], line["level"]) for line in lines] == [
        ("sqlalchemy.engine.Engine", "warning"),
        ("uvicorn.error", "info"),
        ("app.main", "info"),
        ("src.ai.mcp_server.cache", "info"),
        ("some.http.library", "warning"),
        ("py.warnings", "warning"),  # Python's own warnings: lines of the log, not bare text beside it
    ]
    assert "about to be renamed" in lines[-1]["message"]
    assert all(line["service"] == "tripplanner-backend" and "trip_id" in line for line in lines)
    assert sum(isinstance(f, logs.QuietProbes) for f in logging.getLogger("uvicorn.access").filters) == 1


# ── Traces ─────────────────────────────────────────────────────────────────


def test_a_span_is_the_child_of_the_one_it_began_in_and_an_error_marks_it(spans):
    with traces.span("outer", {"run.kind": "plan", "nothing": None, "a list": [1, 2]}) as outer:
        with traces.span("inner", kind=SpanKind.CLIENT):
            pass
        with pytest.raises(ValueError), traces.span("fails"):
            raise ValueError("no flights to Atlantis")
        answered = traces.start("answered, not raised")
        traces.fail(answered, "NOT_FOUND: no such city")
        traces.finish(answered, {"found": 0})

    outer_span, inner, fails, said = (
        named(spans, name) for name in ("outer", "inner", "fails", "answered, not raised")
    )
    assert outer_span.parent is None and dict(outer_span.attributes) == {"run.kind": "plan", "a list": "[1, 2]"}
    assert inner.parent.span_id == outer.get_span_context().span_id and inner.kind is SpanKind.CLIENT
    assert inner.context.trace_id == outer_span.context.trace_id
    assert fails.status.status_code is StatusCode.ERROR and fails.events[0].name == "exception"
    assert said.status.status_code is StatusCode.ERROR and said.status.description == "NOT_FOUND: no such city"
    assert said.attributes["found"] == 0


def test_every_span_says_whose_it_is_whoever_made_it(spans):
    library = trace.get_tracer("some.instrumentation")  # not this package's: SQLAlchemy's, redis-py's
    with context.scope(trip_id=TRIP, user_id=USER):
        with library.start_as_current_span("SELECT tripplanner"):
            pass
    with library.start_as_current_span("startup query"):
        pass

    assert dict(named(spans, "SELECT tripplanner").attributes) == {"trip.id": TRIP, "user.id": USER}
    assert dict(named(spans, "startup query").attributes) == {}


def test_a_trace_crosses_to_another_process_as_one_line_of_text(spans):
    assert traces.inject() == {}  # outside any trace there is nothing to send
    with traces.span("mcp.call search_flights") as sent:
        carrier = traces.inject()
    assert set(carrier) == {"traceparent"} and re.fullmatch(
        r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", carrier["traceparent"]
    )

    # …the other side: what arrives is a dict from JSON, with whatever else was sent beside it
    arrived = {**carrier, "trip_id": TRIP, "progressToken": None}
    with traces.span("tools/call search_flights", kind=SpanKind.SERVER, parent=traces.extract(arrived)):
        pass
    with traces.span("a caller that sent nothing", parent=traces.extract(None)):
        pass

    answered, alone = named(spans, "tools/call search_flights"), named(spans, "a caller that sent nothing")
    assert answered.context.trace_id == sent.get_span_context().trace_id
    assert answered.parent.span_id == sent.get_span_context().span_id and answered.parent.is_remote
    assert alone.parent is None and alone.context.trace_id != answered.context.trace_id


async def test_what_is_set_up_outside_belongs_to_no_trace_and_takes_none_with_it(spans):
    seen = {}

    async def kept_for_the_life_of_the_process() -> None:
        seen["ids"] = traces.current_ids()
        with traces.span("reads the server's answers"):
            pass

    with traces.span("the first request that needed it") as first:
        with traces.outside():
            assert traces.current_ids() == (None, None)
            await asyncio.create_task(kept_for_the_life_of_the_process())
        assert traces.current_ids()[0] == format(first.get_span_context().trace_id, "032x")  # and back

    assert seen["ids"] == (None, None)
    assert named(spans, "reads the server's answers").parent is None


def test_a_block_can_be_left_unrecorded():
    assert is_instrumentation_enabled()
    with traces.unrecorded():
        assert not is_instrumentation_enabled()  # what redis-py's and SQLAlchemy's instrumentation ask before a span
    assert is_instrumentation_enabled()


def test_health_checks_start_no_trace():
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Decision

    sampler = traces.WithoutProbes(ALWAYS_ON)

    def verdict(path_attribute: str, path: str, kind=SpanKind.SERVER):
        return sampler.should_sample(None, 1, "GET", kind, {path_attribute: path}).decision

    for probe in ("/ready", "/ping", "/version", "/metrics", "/ready?verbose=1"):
        assert verdict("http.target", probe) is Decision.DROP, probe
        assert verdict("url.path", probe) is Decision.DROP, probe  # the same attribute under its newer name
    assert verdict("http.target", "/trips") is Decision.RECORD_AND_SAMPLE
    assert (
        verdict("http.target", f"/trips/{TRIP}/ready") is Decision.RECORD_AND_SAMPLE
    )  # a path that only ends like one
    assert (
        verdict("http.target", "/ready", SpanKind.CLIENT) is Decision.RECORD_AND_SAMPLE
    )  # a call *to* somebody's /ready
    assert "WithoutProbes" in sampler.get_description()


def test_nothing_leaves_as_it_was_recorded(spans):
    """The instrumentation writes down a request's whole address and an error's whole message."""
    address = f"https://api.openweathermap.org/data/2.5/forecast?q=Goa&appid={A_PROVIDERS_KEY}"
    stream = f"http://api.example.com/trips/{TRIP}/stream?token={A_LOGIN_TOKEN}"
    with traces.span(
        "GET /trips/{trip_id}/stream",
        {
            "http.url": stream,
            "http.target": stream.split(".com")[1],
            "net.peer.ip": "203.0.113.7",
            "net.peer.port": 51234,
        },
        kind=SpanKind.SERVER,
    ):
        with (
            pytest.raises(RuntimeError),
            traces.span(
                "provider openweather", {"net.peer.name": "db.internal", "net.peer.port": 5432}, kind=SpanKind.CLIENT
            ),
        ):
            raise RuntimeError(f"Client error '401 Unauthorized' for url '{address}'")
    request, provider = named(spans, "GET /trips/{trip_id}/stream"), named(spans, "provider openweather")
    assert (
        A_LOGIN_TOKEN in request.attributes["http.url"] and A_PROVIDERS_KEY in provider.status.description
    )  # as recorded

    kept = InMemorySpanExporter()
    sent = traces.Scrubbed(kept, Scrubber([A_PROVIDERS_KEY]).text)
    sent.export([request, provider])

    clean_request, clean_provider = kept.get_finished_spans()
    everything = json.dumps([json.loads(span.to_json()) for span in (clean_request, clean_provider)])
    assert A_LOGIN_TOKEN not in everything and A_PROVIDERS_KEY not in everything
    assert clean_request.attributes["http.url"].endswith("/stream?token=[Filtered]")
    assert (
        "net.peer.ip" not in clean_request.attributes and "net.peer.port" not in clean_request.attributes
    )  # whose: user.id
    # a client span's peer is a server, not a person: the same attribute names, kept
    assert (clean_provider.attributes["net.peer.name"], clean_provider.attributes["net.peer.port"]) == (
        "db.internal",
        5432,
    )
    assert clean_provider.status.status_code is StatusCode.ERROR and "[Filtered]" in clean_provider.status.description
    message = clean_provider.events[0].attributes
    assert "[Filtered]" in message["exception.message"] and "[Filtered]" in message["exception.stacktrace"]
    # everything else is what it was
    assert (
        clean_request.name,
        clean_request.kind,
        clean_request.context,
        clean_request.start_time,
        clean_request.end_time,
    ) == (
        request.name,
        request.kind,
        request.context,
        request.start_time,
        request.end_time,
    )
    assert clean_provider.parent == provider.parent and clean_provider.resource == provider.resource


def test_a_span_that_cannot_be_scrubbed_is_not_sent(spans, caplog):
    with traces.span("fine"), traces.span("breaks the scrubber", {"note": "poison"}):
        pass

    def scrub(text: str) -> str:
        if text == "poison":
            raise ValueError("cannot read this")
        return text

    kept = InMemorySpanExporter()
    with caplog.at_level(logging.WARNING, logger="src.telemetry.traces"):
        traces.Scrubbed(kept, scrub).export(spans.get_finished_spans())

    assert [span.name for span in kept.get_finished_spans()] == ["fine"]  # a hole in a trace, not a secret in a store
    assert "could not be scrubbed and was not sent" in caplog.text


class _Settings:
    APP_ENV = "production"
    OTEL_EXPORTER_OTLP_ENDPOINT = ""
    OTEL_EXPORTER_OTLP_HEADERS = ""
    OTEL_TRACES_SAMPLE_RATIO = 1.0

    def __init__(self, **values) -> None:
        self.__dict__.update(values)


def test_tracing_is_on_only_with_somewhere_to_send_traces_and_never_in_the_test_stacks(spans, caplog):
    with caplog.at_level(logging.INFO, logger="src.telemetry.traces"):
        assert traces.configure(_Settings(), traces.BACKEND) is False
        assert "tracing is off (OTEL_EXPORTER_OTLP_ENDPOINT is empty)" in caplog.text
        assert traces.enabled() is False  # …and whoever was listening before is not any more

        endpoint = "http://collector.internal:4318/"
        assert (
            traces.configure(_Settings(OTEL_EXPORTER_OTLP_ENDPOINT=endpoint, APP_ENV="test"), traces.BACKEND) is False
        )
        assert "tracing is off (APP_ENV is test)" in caplog.text

        with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter") as exporter:
            on = traces.configure(
                _Settings(
                    OTEL_EXPORTER_OTLP_ENDPOINT="https://user:secret@otlp.example.com:4318/",
                    OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic abc123==, x-scope = team-a ,broken",
                ),
                traces.TOOL_SERVER,
                scrub=str.upper,
            )
    try:
        assert on is True and traces.enabled() is True
        assert exporter.call_args.kwargs == {
            "endpoint": "https://user:secret@otlp.example.com:4318/v1/traces",
            "headers": {"Authorization": "Basic abc123==", "x-scope": "team-a"},
            "timeout": 10,
        }
        listening = traces._switch.to
        assert isinstance(listening, BatchSpanProcessor)
        exporter.return_value.export.return_value = SpanExportResult.SUCCESS
        with traces.span("a request", {"note": "what was recorded"}):
            pass
        listening.force_flush()
        (sent,) = exporter.return_value.export.call_args.args[0]
        assert sent.attributes["note"] == "WHAT WAS RECORDED"  # nothing reaches the exporter but through the scrubber
        assert "service 'tripplanner-mcp', sent to https://otlp.example.com:4318, 100% of requests" in caplog.text
        assert "secret" not in caplog.text  # an endpoint can carry a credential: host and port are logged, no more
    finally:
        traces.shutdown()
    assert traces.enabled() is False


def test_spans_go_to_whoever_is_listening_now(spans):
    with traces.span("heard by the fixture"):
        pass
    second = InMemorySpanExporter()
    traces.use(SimpleSpanProcessor(second))
    with traces.span("heard by the second"):
        pass
    traces.use(None)
    with traces.span("heard by nobody"):
        pass

    assert [span.name for span in second.get_finished_spans()] == ["heard by the second"]
    assert trace.get_tracer_provider() is traces._provider  # one provider for the process, whoever listens


def test_off_is_off_whatever_was_on_before(spans):
    """The provider outlives whoever it hands spans to. With nobody listening, nothing is recorded,
    no log line is given a trace to point at, and nothing is sent along with a call to the tool server."""
    with traces.span("heard") as heard:
        assert heard.is_recording() and traces.current_ids()[0] and set(traces.inject()) == {"traceparent"}

    traces.use(None)
    with traces.span("nobody is listening") as unheard:
        assert unheard.is_recording() is False
        assert traces.current_ids() == (None, None)
        assert traces.inject() == {}
    assert [span.name for span in spans.get_finished_spans()] == ["heard"]


# ── Metrics ────────────────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_cache_hit_ratio_is_of_the_last_five_minutes_and_of_nothing_is_not_zero():
    clock = Clock()
    window = metrics.Window(seconds=300, clock=clock)

    assert math.isnan(window.ratio())  # nobody asked: not "nothing was found"
    window.add(hits=0, lookups=4)  # a trip's caches being warmed: every lookup a miss
    assert window.ratio() == 0.0
    clock.now += 60
    window.add(hits=4, lookups=4)  # the plan a minute later: every lookup a hit
    assert window.ratio() == 0.5
    clock.now += 241  # the warming is more than five minutes ago now
    assert window.ratio() == 1.0
    clock.now += 60
    assert math.isnan(window.ratio())


def test_cache_lookups_are_counted_by_cache_and_by_how_they_ended():
    before = {
        result: metric("mcp_cache_lookups_total", cache="flights", result=result) for result in metrics.CACHE_RESULTS
    }
    with patch.object(metrics, "cache_window", metrics.Window()) as window:
        metrics.cache_lookups("flights", "miss")
        metrics.cache_lookups("flights", "hit", 3)
        metrics.cache_lookups("flights", "error")  # Redis could not be asked: looked up, and not found
        metrics.cache_lookups("flights", "hit", 0)
        metrics.cache_lookups("flights", "who knows")  # not a way a lookup ends: not counted
        assert window.ratio() == 3 / 5

    after = {
        result: metric("mcp_cache_lookups_total", cache="flights", result=result) for result in metrics.CACHE_RESULTS
    }
    assert {result: after[result] - before[result] for result in after} == {"hit": 3, "miss": 1, "error": 1}
    assert metrics.CACHE_HIT_RATIO._value is not None and "mcp_cache_hit_ratio" in generate_latest().decode()


def test_a_model_call_is_counted_with_its_tokens_and_what_they_cost():
    labels = {"agent": "itinerary_builder", "model": "openai/gpt-oss-120b"}
    before = (
        metric("llm_calls_total", status="ok", **labels),
        metric("llm_calls_total", status="error", **labels),
        metric("llm_tokens_total", direction="input", **labels),
        metric("llm_tokens_total", direction="output", **labels),
        metric("llm_cost_usd_total", **labels),
    )

    metrics.llm_call(
        "itinerary_builder", "openai/gpt-oss-120b", ok=True, tokens_in=2000, tokens_out=2400, cost_usd=0.00174
    )
    metrics.llm_call("itinerary_builder", "openai/gpt-oss-120b", ok=False)
    metrics.llm_call(None, None, ok=True)  # asked outside anybody's work, by a model nobody named

    after = (
        metric("llm_calls_total", status="ok", **labels),
        metric("llm_calls_total", status="error", **labels),
        metric("llm_tokens_total", direction="input", **labels),
        metric("llm_tokens_total", direction="output", **labels),
        metric("llm_cost_usd_total", **labels),
    )
    assert [round(now - then, 6) for now, then in zip(after, before)] == [1, 1, 2000, 2400, 0.00174]
    assert metric("llm_calls_total", agent="unmetered", model="unknown", status="ok") >= 1


async def test_a_planning_run_is_timed_and_counted_by_how_it_ended():
    def ended(kind: str, outcome: str) -> float:
        return metric("planning_run_duration_seconds_count", kind=kind, outcome=outcome)

    before = {outcome: ended("refine", outcome) for outcome in metrics.RUN_OUTCOMES}
    in_progress = metric("planning_runs_in_progress")

    with metrics.planning_run("refine"):
        assert metric("planning_runs_in_progress") == in_progress + 1
    with metrics.planning_run("refine") as run:
        run.outcome = "no_plan"  # it ended without an error and without a plan: the block says so
    with pytest.raises(RuntimeError), metrics.planning_run("refine"):
        raise RuntimeError("the builder fell over")
    with pytest.raises(asyncio.CancelledError), metrics.planning_run("refine"):
        raise asyncio.CancelledError  # the server is stopping

    after = {outcome: ended("refine", outcome) for outcome in metrics.RUN_OUTCOMES}
    assert {outcome: after[outcome] - before[outcome] for outcome in after} == {
        "completed": 1,
        "no_plan": 1,
        "crashed": 1,
        "interrupted": 1,
    }
    assert metric("planning_runs_in_progress") == in_progress  # none left counted as running


def test_what_can_be_listed_exists_at_zero_before_it_ever_happens():
    """Prometheus cannot see a counter go from "not there" to 1: a first event has to find a zero to rise from."""
    exposed = generate_latest().decode()

    for status in ("success", "failure", "retry"):
        assert f'itinerary_builds_total{{status="{status}"}}' in exposed
    for kind in ("plan", "replan", "refine", "retry"):
        for outcome in ("completed", "no_plan", "crashed", "interrupted"):
            assert f'planning_run_duration_seconds_count{{kind="{kind}",outcome="{outcome}"}}' in exposed

    metrics.expect_tools(["a_new_tool"], ["a_new_cache"])
    metrics.expect_llm_calls({"a_new_agent": ("models/a-new-model",)})
    assert metric("mcp_tool_calls_total", tool="a_new_tool", status="error") == 0.0
    exposed = generate_latest().decode()
    assert 'mcp_tool_calls_total{status="error",tool="a_new_tool"} 0.0' in exposed
    assert 'mcp_tool_call_duration_seconds_count{tool="a_new_tool"} 0.0' in exposed
    for result in ("hit", "miss", "error"):
        assert f'mcp_cache_lookups_total{{cache="a_new_cache",result="{result}"}} 0.0' in exposed
    for status in ("ok", "error"):  # the error series too: an alert on errors needs its zero
        assert f'llm_calls_total{{agent="a_new_agent",model="a-new-model",status="{status}"}} 0.0' in exposed
    assert "_created" not in exposed  # no second series per counter saying when it was made


def test_the_build_that_is_running_is_a_metric():
    metrics.build_info("0.29.0", "3f2a9c1d5e6b7a8091a2b3c4d5e6f708192a3b4c")
    metrics.build_info("0.29.0", None)

    assert metric("tripplanner_build_info", version="0.29.0", commit="3f2a9c1d5e6b") == 1.0
    assert metric("tripplanner_build_info", version="0.29.0", commit="unknown") == 1.0


# ── A FastAPI app, counted and traced ──────────────────────────────────────


def an_app(may_read=lambda: None) -> FastAPI:
    app = FastAPI()

    @app.get("/phase29/{thing_id}/detail")
    async def detail(thing_id: str) -> dict:
        return {"thing": thing_id}

    @app.get("/phase29/boom")
    async def boom() -> None:
        raise RuntimeError("on purpose")

    @app.get("/phase29/stream")
    async def stream() -> StreamingResponse:
        async def events():
            yield b"data: connected\n\n"
            await asyncio.sleep(0.3)  # an event stream: open long after its first byte
            yield b"data: done\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/ready")
    async def ready() -> dict:
        with traces.span("SELECT 1"):  # what a health check asks the database
            return {"ok": True}

    metrics.instrument_app(app, may_read)
    traces.instrument_app(app)
    return app


async def test_requests_are_counted_per_endpoint_as_the_router_names_it(spans):
    app = an_app()

    def counted(handler: str, status: str) -> float:
        return metric("http_requests_total", handler=handler, method="GET", status=status)

    before = counted("/phase29/{thing_id}/detail", "2xx"), counted("/phase29/boom", "5xx"), counted("none", "4xx")
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as http:
        for thing in ("a1", "b2", TRIP):
            assert (await http.get(f"/phase29/{thing}/detail")).status_code == 200
        assert (await http.get("/phase29/boom")).status_code == 500
        assert (await http.get("/no/such/route")).status_code == 404
        shown = await http.get("/metrics")

    assert counted("/phase29/{thing_id}/detail", "2xx") - before[0] == 3  # one endpoint, whatever the id
    assert counted("/phase29/boom", "5xx") - before[1] == 1
    assert counted("none", "4xx") - before[2] == 1  # an address no route matches is not a name of its own
    assert shown.status_code == 200 and shown.headers["content-type"].startswith("text/plain")
    assert 'http_requests_total{handler="/phase29/{thing_id}/detail",method="GET",status="2xx"}' in shown.text
    assert TRIP not in shown.text and "/phase29/a1/" not in shown.text  # never an address with an id in it
    assert 'handler="/metrics"' not in shown.text  # Prometheus reading the numbers is not traffic
    assert (
        'http_request_duration_seconds_bucket{handler="/phase29/{thing_id}/detail",le="0.025",method="GET"}'
        in shown.text
    )
    assert "/metrics" not in app.openapi()["paths"]


async def test_an_event_stream_is_timed_to_its_first_byte_and_counted_as_in_progress_while_open(spans):
    app = an_app()
    labels = {"handler": "/phase29/stream", "method": "GET"}
    seconds_before = metric("http_request_duration_seconds_sum", **labels)
    open_before = metric("http_requests_inprogress", **labels)
    seen_open = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        request = asyncio.create_task(http.get("/phase29/stream"))
        await asyncio.sleep(0.15)  # the stream has begun and is not over
        seen_open.append(metric("http_requests_inprogress", **labels))
        assert (await request).text.endswith("data: done\n\n")

    assert seen_open == [open_before + 1] and metric("http_requests_inprogress", **labels) == open_before
    assert metric("http_request_duration_seconds_sum", **labels) - seconds_before < 0.2  # not the 0.3 s it stayed open
    stream = named(spans, "GET /phase29/stream")
    assert (stream.end_time - stream.start_time) / 1e9 >= 0.3  # the span is the whole of it
    assert [span.name for span in spans.get_finished_spans()] == ["GET /phase29/stream"]  # no span per event sent


async def test_a_request_is_the_root_of_a_trace_and_a_health_check_of_none(spans):
    app = an_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await http.get("/ready")
        await http.get("/metrics")
        await http.get("/phase29/a1/detail")
        caller = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        await http.get("/phase29/b2/detail", headers={"traceparent": caller})

    made = spans.get_finished_spans()
    assert [span.name for span in made] == [
        "GET /phase29/{thing_id}/detail"
    ] * 2  # the check's own query: no trace either
    first, continued = made
    assert first.parent is None and first.kind is SpanKind.SERVER
    assert first.attributes["http.route"] == "/phase29/{thing_id}/detail"
    assert format(continued.context.trace_id, "032x") == "0af7651916cd43dd8448eb211c80319c"  # a caller's trace goes on


async def test_metrics_are_read_by_whom_the_guard_lets_in(spans):
    from fastapi import HTTPException

    def nobody() -> None:
        raise HTTPException(status_code=404, detail="Not Found")

    async with AsyncClient(transport=ASGITransport(app=an_app(nobody)), base_url="http://test") as http:
        refused = await http.get("/metrics")

    assert refused.status_code == 404 and "http_requests_total" not in refused.text

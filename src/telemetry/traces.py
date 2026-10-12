"""One trace from the request that asked to the row that was written (Phase 29) — OpenTelemetry.

Switched on by one setting, `OTEL_EXPORTER_OTLP_ENDPOINT` (Jaeger's: http://localhost:4318); off,
every span here is one that records nothing and nothing leaves the machine.

A plan, as it reads in Jaeger:

    POST /trips/{trip_id}/plan                    tripplanner-backend   the request — answered 202 at once
      SELECT tripplanner_db                         the login's user, the trip
      PUBLISH                                       "planning_started", to whoever is watching
      planning run                                the background task the request started; it outlives it
        orchestrator
          flight_agent                            every agent_runs row is a span of the same name
            mcp.call search_flights               the call leaves this process…
              tools/call search_flights           tripplanner-mcp   …and is picked up in the tool server
                GET                                 its cache
                provider duffel                     the request to the provider
                SET
          itinerary_builder
            llm openai/gpt-oss-120b               a model call: tokens in and out
          persist
            INSERT tripplanner_db                 the itinerary
            UPDATE tripplanner_db                 the trip, completed

Who makes which span:

- the request, every SQL statement and every Redis command: OpenTelemetry's own instrumentation of
  FastAPI, SQLAlchemy and redis-py (`instrument_app`, `instrument_sqlalchemy`, `instrument_redis`);
- an agent's work, a model call, a tool call, a request to a provider: `span()` and `start()`, called
  where that work is done.

One trace, two processes. A span is its parent's child because the parent is in a context variable
when the child begins, and a task starts with a copy of its starter's context: that carries the
trace from the request into the run it started. It does not cross a process: the tool server is
sent the trace's id with every call (`inject` here, `extract` there — W3C `traceparent`, in the
call's `_meta`).

Health checks are not traced (`WithoutProbes`): a platform asks /ready every few seconds, and a
trace store full of them hides the requests that matter.

What a span may carry. The instrumentation writes down what it sees: a request's whole address —
and an event stream's address ends in `?token=<the login token>` — and, on an error, its message
and stack trace, which for a failed call to a provider include the address that was called, key
and all. So nothing is exported as it was recorded: `Scrubbed` puts every text on every span —
attributes, error events, the status — through the scrubber it is given, which is the one that
cleans the error reports (app/core/error_reporting.py), and leaves a request's span without the
address it came from: whose a request is, is said by `user.id`. SQL is recorded as its statement,
never its values, and a Redis command as its name, never its arguments: that is the libraries' doing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import Decision, ParentBased, Sampler, SamplingResult, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from src.telemetry import context

logger = logging.getLogger(__name__)

BACKEND = "tripplanner-backend"
TOOL_SERVER = "tripplanner-mcp"

# Asked by machines, every few seconds, for ever: never the start of a trace.
PROBES = frozenset({"/ping", "/ready", "/version", "/metrics"})

# What the instrumentation notes about who sent a request: an address is a person's, and is not kept.
WHO_ASKED = frozenset(
    {"net.peer.ip", "net.peer.port", "net.sock.peer.addr", "net.sock.peer.port", "client.address", "client.port"}
)

FLUSH_MILLIS = 5_000  # how long shutdown waits for the last spans to be sent
SEND_EVERY_MILLIS = 2_000  # finished spans leave in batches; this is the longest one waits

_propagator = TraceContextTextMapPropagator()


class WithoutProbes(Sampler):
    """Every request but the health checks is handed to the sampler behind this one.

    A request that is not traced starts no trace, so nothing it does — its `SELECT 1`, its PING —
    is recorded either: `ParentBased` gives a child its parent's verdict.
    """

    def __init__(self, then: Sampler) -> None:
        self._then = then

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None):
        asked = attributes or {}
        path = str(asked.get("url.path") or asked.get("http.target") or "").split("?")[0]
        if kind == SpanKind.SERVER and path in PROBES:
            return SamplingResult(Decision.DROP)
        return self._then.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

    def get_description(self) -> str:
        return f"WithoutProbes({self._then.get_description()})"


class _WhenListening(Sampler):
    """Nothing is recorded while nobody is listening.

    The provider outlives whoever it hands spans to (`use`): after a shutdown, and between two tests,
    it is there and nobody is. Spans made then are not for anyone — so they are not made: off is off,
    whatever was on before.
    """

    def __init__(self, then: Sampler) -> None:
        self._then = then

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None):
        if _switch.to is None:
            return SamplingResult(Decision.DROP)
        return self._then.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

    def get_description(self) -> str:
        return f"WhenListening({self._then.get_description()})"


class _Switch(SpanProcessor):
    """The one processor the provider has: says whose every span is, and hands it to whatever is listening.

    A process may set its tracer provider once. What is done with finished spans has to be changeable
    all the same — nothing, the exporter, a list a test reads — so the provider gets this, and this
    is told where to send them (`use`).
    """

    def __init__(self) -> None:
        self.to: SpanProcessor | None = None

    def on_start(self, span, parent_context=None) -> None:
        known = context.attributes()  # the trip and the user, on every span: any one of them finds the trace
        if known:
            span.set_attributes(known)
        if self.to is not None:
            self.to.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if self.to is not None:
            self.to.on_end(span)

    def shutdown(self) -> None:
        if self.to is not None:
            self.to.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self.to.force_flush(timeout_millis) if self.to is not None else True


class Scrubbed(SpanExporter):
    """Sends spans on to another exporter with every text on them scrubbed — see the module's docstring.

    A span that cannot be scrubbed is not sent: one trace with a hole in it is better than one
    secret in a trace store.
    """

    def __init__(self, to: SpanExporter, scrub: Callable[[str], str]) -> None:
        self._to, self._scrub = to, scrub

    def _texts(self, attributes: Mapping[str, Any] | None) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for name, value in (attributes or {}).items():
            if isinstance(value, str):
                value = self._scrub(value)
            elif isinstance(value, (list, tuple)):
                value = [self._scrub(item) if isinstance(item, str) else item for item in value]
            cleaned[name] = value
        return cleaned

    def clean(self, span: ReadableSpan) -> ReadableSpan:
        attributes = self._texts(span.attributes)
        if span.kind == SpanKind.SERVER:
            attributes = {name: value for name, value in attributes.items() if name not in WHO_ASKED}
        events = [Event(event.name, self._texts(event.attributes), event.timestamp) for event in span.events]
        described = span.status.description
        status = Status(span.status.status_code, self._scrub(described)) if described else span.status
        return ReadableSpan(
            name=span.name,
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=attributes,
            events=events,
            links=span.links,
            kind=span.kind,
            status=status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        fit_to_send = []
        for span in spans:
            try:
                fit_to_send.append(self.clean(span))
            except Exception:
                logger.warning("[OTEL] a span could not be scrubbed and was not sent", exc_info=True)
        return self._to.export(fit_to_send)

    def shutdown(self) -> None:
        self._to.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._to.force_flush(timeout_millis)


_switch = _Switch()
_provider: TracerProvider | None = None


def _where(endpoint: str) -> str:
    """An endpoint as it may be logged: scheme, host and port — an address can carry a credential."""
    try:
        parts = urlsplit(endpoint)
        return f"{parts.scheme}://{parts.hostname or '?'}{f':{parts.port}' if parts.port else ''}"
    except ValueError:
        return "an unreadable URL"


def _headers(text: str) -> dict[str, str]:
    """`OTEL_EXPORTER_OTLP_HEADERS` as it is written everywhere: name=value,name=value."""
    pairs = (pair.split("=", 1) for pair in (text or "").split(",") if "=" in pair)
    return {name.strip(): value.strip() for name, value in pairs if name.strip()}


def configure(settings: Any, service: str, version: str = "", scrub: Callable[[str], str] | None = None) -> bool:
    """Switch tracing on if the settings say where to send traces. Returns whether it is on.

    `scrub`: what every text on a span is put through before it is sent (`Scrubbed`).
    `APP_ENV=test` keeps tracing off whatever `.env` says: the test stacks make hundreds of
    requests, and none of them belongs in anyone's trace store.
    """
    endpoint = (getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "") or "").strip().rstrip("/")
    environment = str(getattr(settings, "APP_ENV", "") or "")
    off = None
    if not endpoint:
        off = "OTEL_EXPORTER_OTLP_ENDPOINT is empty"
    elif environment == "test":
        off = "APP_ENV is test"
    if off:
        use(None)
        logger.info("[OTEL] tracing is off (%s)", off)
        return False

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    ratio = min(1.0, max(0.0, float(getattr(settings, "OTEL_TRACES_SAMPLE_RATIO", 1.0))))
    exporter: SpanExporter = OTLPSpanExporter(
        endpoint=f"{endpoint}/v1/traces",
        headers=_headers(getattr(settings, "OTEL_EXPORTER_OTLP_HEADERS", "")) or None,
        timeout=10,
    )
    if scrub is not None:
        exporter = Scrubbed(exporter, scrub)
    use(
        BatchSpanProcessor(exporter, schedule_delay_millis=SEND_EVERY_MILLIS),
        service=service,
        version=version,
        environment=environment,
        ratio=ratio,
    )
    logger.info(
        "[OTEL] tracing is on — service %r, sent to %s, %.0f%% of requests", service, _where(endpoint), ratio * 100
    )
    return True


def use(
    processor: SpanProcessor | None,
    service: str = BACKEND,
    version: str = "",
    environment: str = "",
    ratio: float = 1.0,
) -> None:
    """Hand finished spans to this processor from now on; None: to nobody. `configure` and the tests end up here.

    The provider is made the first time there is somebody to hand spans to, and stays: the name of
    the service and the share of requests traced are decided then.
    """
    global _provider
    if _provider is None and processor is not None:
        described = {"service.name": service, "service.namespace": "tripplanner"}
        if version:
            described["service.version"] = version
        if environment:
            described["deployment.environment"] = environment
        _provider = TracerProvider(
            resource=Resource.create(described),
            sampler=_WhenListening(ParentBased(root=WithoutProbes(TraceIdRatioBased(ratio)))),
        )
        _provider.add_span_processor(_switch)
        trace.set_tracer_provider(_provider)
    before, _switch.to = _switch.to, processor
    if before is not None and before is not processor:
        before.shutdown()


def enabled() -> bool:
    return _switch.to is not None


def shutdown() -> None:
    """Send what is still waiting — for a few seconds at most — and stop."""
    if _switch.to is None:
        return
    try:
        if not _switch.to.force_flush(FLUSH_MILLIS):
            logger.warning("[OTEL] the last spans were not sent within %.0f s", FLUSH_MILLIS / 1000)
    except Exception:
        logger.warning("[OTEL] the last spans could not be sent", exc_info=True)
    use(None)


# ── Spans made here ────────────────────────────────────────────────────────


def _tracer() -> trace.Tracer:
    return trace.get_tracer("tripplanner")


def _plain(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Attributes a span can hold: text, numbers and booleans — anything else as text, nothing for None."""
    plain: dict[str, Any] = {}
    for name, value in (attributes or {}).items():
        if value is None:
            continue
        plain[name] = value if isinstance(value, (str, bool, int, float)) else str(value)
    return plain


@contextmanager
def span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    parent: otel_context.Context | None = None,
) -> Iterator[trace.Span]:
    """A span around a block: whatever begins inside is its child. An error that leaves the block marks it failed.

    `parent`: the context to continue instead of the one under way — what a caller in another
    process sent (`extract`).
    """
    with _tracer().start_as_current_span(name, context=parent, kind=kind, attributes=_plain(attributes)) as current:
        yield current


def start(name: str, attributes: Mapping[str, Any] | None = None, *, kind: SpanKind = SpanKind.INTERNAL) -> trace.Span:
    """A span for work that begins in one call and ends in another (a model call's two callbacks).

    Whoever starts it ends it: `finish`. Nothing begun meanwhile becomes its child.
    """
    return _tracer().start_span(name, kind=kind, attributes=_plain(attributes))


def finish(
    current: trace.Span, attributes: Mapping[str, Any] | None = None, error: BaseException | None = None
) -> None:
    if attributes:
        current.set_attributes(_plain(attributes))
    if error is not None:
        current.record_exception(error)
        current.set_status(Status(StatusCode.ERROR, f"{type(error).__name__}: {str(error)[:200]}"))
    current.end()


def fail(current: trace.Span, why: str) -> None:
    """Mark a span failed for something that was answered and not raised — a tool's error result."""
    current.set_status(Status(StatusCode.ERROR, why[:200]))


def current_ids() -> tuple[str | None, str | None]:
    """(trace id, span id) of the span under way, as Jaeger writes them; (None, None) outside any trace.

    And inside one that is not recorded — a request the sampler passed over: an id on a log line is
    for opening the trace, and there is none to open.
    """
    here = trace.get_current_span().get_span_context()
    if not here.is_valid or not here.trace_flags.sampled:
        return None, None
    return format(here.trace_id, "032x"), format(here.span_id, "016x")


def unrecorded():
    """A block whose Redis commands and SQL statements make no spans.

    For what happens hundreds of times and says nothing each time: the pieces of an itinerary
    published as the model writes them, twenty a second.
    """
    return suppress_instrumentation()


@contextmanager
def outside() -> Iterator[None]:
    """A block that belongs to no trace: what begins inside is nobody's child, and a task started
    inside does not carry the trace under way with it. For what outlives the request that happens
    to set it up — a connection kept for the life of the process."""
    token = otel_context.attach(otel_context.Context())
    try:
        yield
    finally:
        otel_context.detach(token)


# ── From one process to the next ───────────────────────────────────────────


def inject() -> dict[str, str]:
    """The trace under way, as what a callee needs to continue it: {"traceparent": …}.

    Empty outside a trace, and with tracing off. A request the sampler passed over is sent too, as
    passed over: the callee must not start a trace of its own for half of a request.
    """
    carrier: dict[str, str] = {}
    if enabled():
        _propagator.inject(carrier)
    return carrier


def extract(carrier: Mapping[str, Any] | None) -> otel_context.Context:
    """The context a caller sent. A carrier with nothing in it gives a context with no trace: a new one begins."""
    sent = {name: value for name, value in (carrier or {}).items() if isinstance(value, str)}
    return _propagator.extract(sent)


# ── OpenTelemetry's own instrumentation ────────────────────────────────────


def instrument_app(app: Any) -> None:
    """Every request to a FastAPI app is the root of a trace (or continues the caller's).

    Without the `receive` and `send` spans: an event stream sends a message per event, and each
    would be a span of its own.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="", exclude_spans=["receive", "send"])


def instrument_sqlalchemy() -> None:
    """Every engine made after this reports its statements as spans. Safe to call more than once.

    It is the *making* of an engine that is instrumented — `sqlalchemy.ext.asyncio.create_async_engine`
    is replaced in its module — so an engine has to be made through the module, by a name looked up
    when it is called (app/db/session.py), not through a name imported before this ran.
    """
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    instrumentor = SQLAlchemyInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument()


def instrument_redis(client: Any) -> None:
    """This Redis client's commands as spans: the command's name, never its arguments."""
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    RedisInstrumentor.instrument_client(client)

r"""
MCP server — Phase 3 (real APIs, Redis caching), Phase 24 (the tools run side by side).

Run standalone:
    python -m src.ai.mcp_server.server

Inspect via MCP Inspector:
    npx @modelcontextprotocol/inspector python -m src.ai.mcp_server.server

Phase 29 — this is a process of its own, and a call into it is still part of the request that
caused it. A caller may send three things beside a call's arguments, in the call's `_meta`:

    traceparent   the trace to continue: the work here becomes spans of the caller's trace
    trip_id       \ whose the work is: on every log line written while the call is answered,
    user_id       / and on every span

and is told one thing beside the answer: what the cache did for the call (models.CACHE_REPORT).
A caller that sends nothing — the MCP Inspector — gets the same answers; its calls are traces of
their own, about nobody's trip.
"""

import functools
import logging
import sys
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from opentelemetry.trace import SpanKind

from src.ai.mcp_server import cache
from src.ai.mcp_server.config import mcp_settings
from src.ai.mcp_server.models import CACHE_REPORT, ToolError
from src.ai.mcp_server.tools import (
    estimate_budget,
    get_attractions,
    get_weather,
    search_flights,
    search_hotels,
)
from src.telemetry import context as telemetry_context
from src.telemetry import logs, traces

logger = logging.getLogger(__name__)

TOOLS = (search_flights, search_hotels, get_attractions, get_weather, estimate_budget)


def _sent_with_call() -> dict[str, Any]:
    """What the caller sent beside the arguments of the call being answered; nothing outside a call."""
    try:
        meta = mcp.get_context().request_context.meta
    except Exception:  # not inside a request: a test calling the tool as a function
        return {}
    return meta.model_dump() if meta is not None else {}


async def _report(tally: cache.Tally) -> None:
    """Tell the caller what the cache did for this call. Never fails a call: the answer matters more."""
    counted = tally.counted()
    if not counted:
        return
    try:
        asked = mcp.get_context()
        await asked.session.send_log_message(
            level="info", data={"lookups": counted}, logger=CACHE_REPORT, related_request_id=asked.request_id
        )
    except Exception:
        logger.debug("The cache's tally could not be sent with the answer", exc_info=True)


def threaded(tool: Callable[..., Any]) -> Callable[..., Any]:
    """The same tool, run in a worker thread.

    FastMCP calls a plain function on its event loop, and the tools block — an
    HTTP request each, and now a wait for a rate limit or a backoff. So the
    three searches a plan sends together were answered one after another, and
    none of the answers left until the last was ready: a 1-second attractions
    search took as long as the 5-second hotel search beside it. As a coroutine
    that hands the work to a thread, each call is answered when it is done.

    functools.wraps keeps the name, the docstring and the signature, which is
    what FastMCP builds the tool's schema from: callers see no difference.
    """

    @functools.wraps(tool)
    async def run(*args: Any, **kwargs: Any) -> Any:
        # Phase 29: the call is a span of its caller's trace, and the work is known to be for the
        # caller's trip. The worker thread starts with a copy of this context — so its cache
        # commands and its requests to a provider are this span's children, its log lines carry
        # the trip, and its cache lookups land in this tally.
        sent, tally = _sent_with_call(), cache.Tally()
        with (
            telemetry_context.scope(trip_id=sent.get("trip_id"), user_id=sent.get("user_id")),
            traces.span(
                f"tools/call {tool.__name__}",
                {"rpc.system": "mcp", "rpc.method": "tools/call", "mcp.tool.name": tool.__name__},
                kind=SpanKind.SERVER,
                parent=traces.extract(sent),
            ) as span,
            cache.counting(tally),
        ):
            result = await anyio.to_thread.run_sync(functools.partial(tool, *args, **kwargs))
            if isinstance(result, ToolError):
                traces.fail(span, f"{result.code}: {result.error}")
            await _report(tally)
        return result

    return run


def _as_a_process() -> None:
    """Phase 29 — the process's own logging and tracing, by the settings it shares with the app that started it.

    Standard error, never standard output: that one is the protocol.
    """
    try:
        from app.core.error_reporting import Scrubber, secrets_of
    except ImportError:
        from src.backend.app.core.error_reporting import Scrubber, secrets_of

    scrub = Scrubber(secrets_of(mcp_settings)).text
    logs.redact_every_record(scrub)  # Phase 35: every line, text or JSON
    logs.configure(
        logs.chosen(mcp_settings.LOG_FORMAT, mcp_settings.APP_ENV), traces.TOOL_SERVER, stream=sys.stderr, scrub=scrub
    )
    traces.configure(mcp_settings, traces.TOOL_SERVER, scrub=scrub)


if __name__ == "__main__":
    _as_a_process()  # before the server object is made: what a library says as it is made is a log line too

mcp = FastMCP("tripplanner-ai")

for _tool in TOOLS:
    mcp.add_tool(threaded(_tool))

if __name__ == "__main__":
    try:
        mcp.run()
    finally:
        traces.shutdown()

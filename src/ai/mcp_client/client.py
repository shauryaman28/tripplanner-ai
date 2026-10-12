"""MCP client wrapper — Phase 6 Dev B.

Provides a unified interface for calling MCP tools on the local FastMCP server.
Handles session lifecycle, argument wrapping, and error parsing.

Session lifecycle notes:
- get_session() lazily starts the MCP server as a subprocess (stdio transport)
  and keeps one persistent ClientSession alive for the process lifetime.
- The global is only committed to _session/_client_context after
  initialize() succeeds — if startup fails partway through, nothing is
  leaked into the globals, so the next call retries cleanly instead of
  reusing a half-initialized session forever.
- close_session() should be wired into FastAPI's lifespan shutdown
  (same pattern as init_redis/close_redis in app.main) so the subprocess
  doesn't leak across app restarts during dev.

Phase 29 — the tool server is another process, and what it does for a call is
still part of the request that made the call:
- every call is counted and timed (mcp_tool_calls_total, …_duration_seconds) and
  is a span, "mcp.call <tool>";
- the trace it belongs to, and the trip and user it is for, go with it in the
  call's `_meta` — the tool server continues the one and logs under the others;
- what the server's cache did for the call comes back beside the answer, as a
  log message of the protocol's own (models.CACHE_REPORT), and is counted here:
  mcp_cache_lookups_total, mcp_cache_hit_ratio.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from opentelemetry.trace import SpanKind

from src.ai import tracing
from src.telemetry import context as telemetry_context
from src.telemetry import metrics, traces

try:
    from src.ai.mcp_server.models import CACHE_REPORT, CACHES, TOOL_NAMES, ToolError
except ImportError:
    from ai.mcp_server.models import CACHE_REPORT, CACHES, TOOL_NAMES, ToolError

metrics.expect_tools(TOOL_NAMES, CACHES)  # every tool's and every cache's counter exists before its first event

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_session: ClientSession | None = None
_client_context: Any = None
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Get or create the module-level lock lazily within the active event loop."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def _on_server_log(params: Any) -> None:
    """What the tool server says beside its answers. One thing is read: what its cache did for a call."""
    if getattr(params, "logger", None) != CACHE_REPORT:
        return
    data = getattr(params, "data", None)
    lookups = data.get("lookups") if isinstance(data, dict) else None
    for cache, results in (lookups if isinstance(lookups, dict) else {}).items():
        for result, count in (results if isinstance(results, dict) else {}).items():
            try:
                metrics.cache_lookups(str(cache), str(result), int(count))
            except (TypeError, ValueError):
                continue


def _sent_with_call() -> dict[str, str]:
    """What goes with a call beside its arguments: the trace to continue, and whose the work is."""
    whose = {"trip_id": telemetry_context.trip_id(), "user_id": telemetry_context.user_id()}
    return {**traces.inject(), **{name: value for name, value in whose.items() if value}}


async def get_session() -> ClientSession:
    """Get or create the persistent MCP client session.

    Fast path avoids the lock once a session exists (call_tool is called
    concurrently by fanned-out agents from Phase 9 onward). The lock only
    guards the one-time creation race.
    """
    global _session, _client_context

    if _session is not None:
        return _session

    async with _get_lock():
        if _session is not None:  # re-check after acquiring the lock
            return _session

        # Run the server in a subprocess using the current Python executable
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "src.ai.mcp_server.server"],
            env=os.environ.copy(),
            cwd=_PROJECT_ROOT,  # `-m src.ai…` only resolves from the repo root
        )

        # Start stdio client connection. Nothing is assigned to the module
        # globals until every step below succeeds — if initialize() raises,
        # this local state is simply discarded and the next call retries
        # from scratch instead of reusing a broken session.
        # Phase 29: the session's own tasks — the ones that read the server's answers for as long as
        # the process lives — start with a copy of the context they are made in. Made in the context
        # of whichever call happened to come first, they would log under that trip, and as children
        # of that request, for ever. So the session is made in nobody's.
        with telemetry_context.scope(), traces.outside():
            # errlog: where the server's own log lines go — this process's standard error, as it is now.
            # (Left out, it is the one the library saw when it was imported.)
            client_context = stdio_client(server_params, errlog=sys.stderr)
            read, write = await client_context.__aenter__()
            session = None
            try:
                session = ClientSession(read, write, logging_callback=_on_server_log)
                await session.__aenter__()
                await session.initialize()

                _client_context = client_context
                _session = session
                return _session
            except Exception:
                if session is not None:
                    try:
                        await session.__aexit__(None, None, None)
                    except Exception:
                        pass
                try:
                    await client_context.__aexit__(None, None, None)
                except Exception:
                    pass
                raise


async def close_session() -> None:
    """Close the active MCP client session and clean up resources."""
    global _session, _client_context
    async with _get_lock():
        if _session is not None:
            try:
                await _session.__aexit__(None, None, None)
            except Exception:
                pass
            _session = None
        if _client_context is not None:
            try:
                await _client_context.__aexit__(None, None, None)
            except Exception:
                pass
            _client_context = None


def _parse_result(response: Any) -> Any:
    """Return the tool's value as plain JSON data.

    FastMCP sends a list result as one text block PER ITEM, so reading only the
    first block would silently truncate every list to its first element. The
    structured payload (wrapped as {"result": ...} for non-object returns) is
    exact; the text blocks are only a fallback for servers that don't send it.
    """
    structured = getattr(response, "structuredContent", None)
    if isinstance(structured, dict):
        return structured["result"] if set(structured) == {"result"} else structured

    texts = [block.text for block in response.content if getattr(block, "text", None) is not None]
    if not texts:
        raise ValueError("response has no text content")
    items = [json.loads(text) for text in texts]
    return items[0] if len(items) == 1 else items


async def call_tool(tool_name: str, params: dict) -> dict | list | ToolError:
    """Call an MCP tool asynchronously. Returns a Pydantic ToolError for any error case — never raises.

    Phase 26: inside a traced run the call is a span of its own — "mcp:search_flights",
    with what was asked and what came back. A tool that could not answer is a span
    that failed: nothing is raised here, so the span is told — otherwise the one call
    that explains why a re-plan gave up would look like every call that worked. A call
    that belongs to no run (a trip's caches being warmed) is not traced: it would be a
    trace of one span, about nothing.
    """
    with (
        tracing.span(f"mcp:{tool_name}", run_type="tool", inputs=params, needs_parent=True) as span,
        traces.span(
            f"mcp.call {tool_name}",
            {"rpc.system": "mcp", "rpc.method": "tools/call", "mcp.tool.name": tool_name},
            kind=SpanKind.CLIENT,
        ) as sent,
    ):
        started = time.perf_counter()
        result = await _call_tool(tool_name, params)
        failed = isinstance(result, ToolError)
        metrics.tool_call(tool_name, ok=not failed, seconds=time.perf_counter() - started)
        if failed:
            span.end(outputs={"error": result.error, "code": result.code}, error=f"{result.code}: {result.error}")
            sent.set_attribute("error.type", result.code)
            traces.fail(sent, f"{result.code}: {result.error}")
        else:
            span.end(outputs={"result": result})
        return result


async def _call_tool(tool_name: str, params: dict) -> dict | list | ToolError:
    """The call itself.

    Ensures parameters are nested under the 'input' key (as expected by FastMCP,
    since every tool function has a single Pydantic-model parameter named
    'input'), handles network / server errors, and parses responses.
    """
    try:
        session = await get_session()

        # FastMCP tools wrap their inputs in an 'input' schema parameter
        arguments = params if "input" in params else {"input": params}
        with_it = _sent_with_call()  # Phase 29; nothing to send → the call as it always was
        response = await session.call_tool(tool_name, arguments=arguments, **({"meta": with_it} if with_it else {}))

        if getattr(response, "isError", False):
            error_msg = getattr(response.content[0], "text", str(response.content[0])) if response.content else ""
            return ToolError(error=f"MCP error: {error_msg}", code="MCP_SERVER_ERROR")

        try:
            parsed = _parse_result(response)
        except ValueError as exc:  # includes json.JSONDecodeError
            return ToolError(error=f"Unparseable MCP response: {exc}", code="INVALID_RESPONSE")

        # Convert tool error dict responses into ToolError model
        if isinstance(parsed, dict) and "error" in parsed and "code" in parsed:
            return ToolError(**parsed)

        return parsed

    except Exception as exc:
        return ToolError(error=f"MCP client exception: {exc}", code="MCP_CLIENT_ERROR")

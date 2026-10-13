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

Phase 31 — the tool server can die, and this process must outlive it. The session
used to be made once and kept for ever: killed in the middle of a plan
(docs/chaos-report.md), the tool server took every later search with it — each
failed at once with an empty "MCP client exception: ", and went on failing until
the backend itself was restarted. Now:
- a call that finds the connection gone forgets that session, so that the next
  call starts a new tool server;
- and asks once more itself, of the new one — a search changes nothing, so it
  can be asked twice. A tool server that dies again under the second asking is
  not asked a third time;
- what the caller is then told has a name: CONNECTION_REFUSED, "the tool server
  could not be reached" — whether it died under the call or would not start;
- every start is counted (mcp_tool_server_starts_total): one is the process
  starting, any more are a tool server that died.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED
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

logger = logging.getLogger(__name__)

# Phase 31: what a call is told when the tool server could not be reached — it died under the call,
# it was already dead, or a new one would not start. (The roadmap's name for it.)
SERVER_GONE = "CONNECTION_REFUSED"
CLEANUP_S = 5.0  # how long a dead tool server's pipes and process are given to be put away
# What a pipe to a process that is no longer there raises, on the way in or on the way out.
_PIPE_IS_CLOSED = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
    BrokenPipeError,
    ConnectionError,
)


class _ServerGone(Exception):
    """The tool server's pipe closed — before a call was sent, or while its answer was waited for."""


def _gone(exc: BaseException) -> bool:
    """Whether an error from a call means the tool server is not there any more."""
    if isinstance(exc, _ServerGone):
        return True
    if isinstance(exc, McpError):  # what the library answers every pending request with when the pipe closes
        return getattr(exc.error, "code", None) == CONNECTION_CLOSED
    return isinstance(exc, _PIPE_IS_CLOSED)


_session: ClientSession | None = None
_client_context: Any = None
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_lock() -> asyncio.Lock:
    """The lock that guards making and letting go of the session — one per event loop.

    A lock that tasks have waited on belongs to the loop they waited in. Since Phase 31 calls do
    wait on it (three searches finding the tool server dead together), so a process that runs more
    than one loop in its life — the test suite does — needs a lock for each.
    """
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
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


class _ToolServer:
    """One tool server process and the session to it — opened, held and closed by one task of its own.

    Phase 31. The pipes and the session are scopes of the library's, and a scope has to be left by
    the task that entered it: left by another, it cancels the one that entered. That task used to
    be whichever request needed a search first — so putting a dead tool server away in the middle
    of things would have cancelled somebody's planning run. Here a task that does nothing else
    enters them, waits to be told to leave, and leaves.

    It is made in nobody's context (Phase 29): the session's own tasks — the ones that read the
    server's answers for as long as it lives — start with a copy of the context they are made in.
    Made in the context of whichever call happened to come first, they would log under that trip,
    and as children of that request, for ever.
    """

    def __init__(self) -> None:
        self.session: ClientSession | None = None
        self.gone = asyncio.Event()  # its pipe has closed: the process is dead, whatever the session believes
        self._up = asyncio.Event()  # the session is ready — or could not be made
        self._leave = asyncio.Event()
        self._failed: BaseException | None = None
        self._owner: asyncio.Task | None = None

    async def _pass_on(self, read: Any, heard: Any) -> None:
        """Everything the server says, handed to the session as it comes — and the moment it stops, noticed.

        The session reads the server's answers in a task of its own, and when the pipe closes it
        fails the requests that were waiting. A request sent a moment *after* that is never
        answered and never failed: it waits for ever (seen: a tool server killed again while it
        was being started for three calls at once — two were told, the third hung). So the pipe
        is watched here, by something a call can wait on beside its answer.
        """
        try:
            async with heard:
                async for message in read:
                    await heard.send(message)
        except Exception:  # a pipe that is being closed says so in several ways; all of them are "it stopped"
            pass
        finally:
            self.gone.set()

    async def _own(self) -> None:
        # Run the server in a subprocess using the current Python executable
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "src.ai.mcp_server.server"],
            env=os.environ.copy(),
            cwd=_PROJECT_ROOT,  # `-m src.ai…` only resolves from the repo root
        )
        try:
            # errlog: where the server's own log lines go — this process's standard error, as it is now.
            # (Left out, it is the one the library saw when it was imported.)
            async with stdio_client(server_params, errlog=sys.stderr) as (read, write):
                heard_from_here, heard = anyio.create_memory_object_stream(0)
                relay = asyncio.create_task(self._pass_on(read, heard_from_here), name="tool server's answers")
                try:
                    session = ClientSession(heard, write, logging_callback=_on_server_log)
                    async with session:
                        await session.initialize()
                        self.session = session
                        self._up.set()
                        await self._leave.wait()
                finally:
                    relay.cancel()
        except asyncio.CancelledError as exc:
            # Two things cancel this task. Somebody stopping it (stop(), or the event loop closing): that
            # is passed on. And the library's own scope, when writing to a dead server's pipe fails in one
            # of its tasks — it cancels its host, which is this task: that is only how the server's end
            # reaches here, and by now the scopes above have been left as they must be.
            self._failed = exc
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
        except Exception as exc:  # it would not start; or the pipes complained as they were closed
            self._failed = exc
        finally:
            self.session = None
            self.gone.set()
            self._up.set()

    async def start(self) -> ClientSession:
        """The session, once the server has answered `initialize`. Raises what stopped it, with nothing left behind."""
        with telemetry_context.scope(), traces.outside():
            self._owner = asyncio.create_task(self._own(), name="tool server")
        await self._up.wait()
        if self.session is None:
            await self.stop()
            raise self._failed or RuntimeError("the tool server did not start")
        return self.session

    async def stop(self) -> None:
        """Have the owning task close everything, and wait for it — a little; a dead server's pipes are not waited on for ever."""
        self._leave.set()
        if self._owner is not None and not self._owner.done():
            done, _ = await asyncio.wait({self._owner}, timeout=CLEANUP_S)
            if not done:
                self._owner.cancel()


async def get_session() -> ClientSession:
    """Get or create the persistent MCP client session.

    Fast path avoids the lock once a session exists (call_tool is called
    concurrently by fanned-out agents from Phase 9 onward). The lock only
    guards the creation race — the first one, and (Phase 31) each one after a
    tool server has died.

    Nothing is assigned to the module globals until the server has answered:
    if it will not start, nothing is kept, and the next call tries from scratch
    instead of reusing a broken session.
    """
    global _session, _client_context

    if _session is not None:
        return _session

    async with _get_lock():
        if _session is not None:  # re-check after acquiring the lock
            return _session
        server = _ToolServer()
        session = await server.start()
        _client_context, _session = server, session
        metrics.TOOL_SERVER_STARTS.inc()  # Phase 31: one is this process starting; more, a tool server that died
        return _session


async def close_session() -> None:
    """Close the active MCP client session and clean up resources."""
    global _session, _client_context
    async with _get_lock():
        server, _session, _client_context = _client_context, None, None
    if server is not None:
        await server.stop()


async def _forget(session: ClientSession | None) -> None:
    """The tool server behind `session` is gone: let go of it, so that the next call starts another.

    Several calls can find the same server dead at the same moment (a plan's searches run side by
    side). Only the first lets go; by the time the others look, the session is no longer theirs to
    forget — it is the new one, or nothing.
    """
    global _session, _client_context
    async with _get_lock():
        if session is None or _session is not session:
            return
        server, _session, _client_context = _client_context, None, None
    if server is not None:
        await server.stop()  # its pipes, and what is left of its process


async def _ask(session: ClientSession, tool_name: str, arguments: dict, with_it: dict[str, str]) -> Any:
    """One call, waited for only as long as the tool server it was sent to is there."""
    server = _client_context if getattr(_client_context, "session", None) is session else None
    extra = {"meta": with_it} if with_it else {}  # Phase 29; nothing to send → the call as it always was
    if server is None:  # a session nobody here is watching the pipe of
        return await session.call_tool(tool_name, arguments=arguments, **extra)
    if server.gone.is_set():
        raise _ServerGone("its pipe was already closed")

    asking = asyncio.ensure_future(session.call_tool(tool_name, arguments=arguments, **extra))
    watching = asyncio.ensure_future(server.gone.wait())
    try:
        await asyncio.wait({asking, watching}, return_when=asyncio.FIRST_COMPLETED)
        if asking.done():
            return asking.result()
        raise _ServerGone("its pipe closed before it answered")
    finally:
        for waited in (asking, watching):
            if not waited.done():
                waited.cancel()


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
    # FastMCP tools wrap their inputs in an 'input' schema parameter
    arguments = params if "input" in params else {"input": params}
    response = None
    for asking in ("first", "again"):
        session = None
        try:
            session = await get_session()
            response = await _ask(session, tool_name, arguments, _sent_with_call())
            break
        except Exception as exc:
            # Phase 31: the server died under the call, was dead already, or would not start.
            if not (_gone(exc) or session is None):
                return ToolError(error=f"MCP client exception: {type(exc).__name__}: {exc}", code="MCP_CLIENT_ERROR")
            await _forget(session)
            what = "would not start" if session is None else "is gone"
            if asking == "again":
                logger.error("The tool server %s a second time during %s (%s) — not asked again", what, tool_name, exc)
                return ToolError(error="The tool server could not be reached.", code=SERVER_GONE)
            logger.warning(
                "The tool server %s during %s (%s) — starting another and asking again", what, tool_name, exc
            )

    try:
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

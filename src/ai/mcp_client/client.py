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
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:
    from src.ai.mcp_server.models import ToolError
except ImportError:
    from ai.mcp_server.models import ToolError

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
        client_context = stdio_client(server_params)
        read, write = await client_context.__aenter__()
        session = None
        try:
            session = ClientSession(read, write)
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
    """Call an MCP tool asynchronously.

    Ensures parameters are nested under the 'input' key (as expected by FastMCP,
    since every tool function has a single Pydantic-model parameter named
    'input'), handles network / server errors, and parses responses. Returns a
    Pydantic ToolError for any error case — never raises.
    """
    try:
        session = await get_session()

        # FastMCP tools wrap their inputs in an 'input' schema parameter
        arguments = params if "input" in params else {"input": params}
        response = await session.call_tool(tool_name, arguments=arguments)

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

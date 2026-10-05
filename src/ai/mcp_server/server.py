"""
MCP server — Phase 3 (real APIs, Redis caching), Phase 24 (the tools run side by side).

Run standalone:
    python -m src.ai.mcp_server.server

Inspect via MCP Inspector:
    npx @modelcontextprotocol/inspector python -m src.ai.mcp_server.server
"""

import functools
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

from src.ai.mcp_server.tools import (
    estimate_budget,
    get_attractions,
    get_weather,
    search_flights,
    search_hotels,
)

TOOLS = (search_flights, search_hotels, get_attractions, get_weather, estimate_budget)


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
        return await anyio.to_thread.run_sync(functools.partial(tool, *args, **kwargs))

    return run


mcp = FastMCP("tripplanner-ai")

for _tool in TOOLS:
    mcp.add_tool(threaded(_tool))

if __name__ == "__main__":
    mcp.run()

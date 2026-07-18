"""
MCP server — Phase 3 (real APIs, Redis caching).

Run standalone:
    python -m src.ai.mcp_server.server

Inspect via MCP Inspector:
    npx @modelcontextprotocol/inspector python -m src.ai.mcp_server.server
"""

from mcp.server.fastmcp import FastMCP

from src.ai.mcp_server.tools import (
    estimate_budget,
    get_attractions,
    get_weather,
    search_flights,
    search_hotels,
)

mcp = FastMCP("tripplanner-ai")

mcp.add_tool(search_flights)
mcp.add_tool(search_hotels)
mcp.add_tool(get_attractions)
mcp.add_tool(get_weather)
mcp.add_tool(estimate_budget)

if __name__ == "__main__":
    mcp.run()

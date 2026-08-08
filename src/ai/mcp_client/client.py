"""
MCP client wrapper — Phase 6, Dev A's temporary stub.

CONTRACT (this is what Dev B builds the real version against):

    async def call_tool(tool_name: str, params: dict) -> list[dict] | dict | ToolError

    - Success -> the tool's Pydantic output(s), converted to plain
      dict / list[dict] via .model_dump(). Agents only ever see
      JSON-serialisable data, never a Pydantic model instance.
    - Failure -> a ToolError instance. Never a raw dict shaped like an
      error, never a raised exception. Callers can always safely do
      `isinstance(result, ToolError)`.

WHY THIS FILE IS TEMPORARY:
It calls the Phase 3 MCP tool functions directly, in-process — it does
NOT speak the actual MCP protocol or talk to a running MCP server.
Dev B is expected to replace the body of call_tool() with a real MCP
client. As long as the function signature and the dict/ToolError
contract above stay identical, no agent code (FlightAgent, and later
HotelAgent / ActivitiesAgent) should need to change.
"""

import asyncio
from typing import Any

from src.ai.mcp_server import tools as mcp_tools
from src.ai.mcp_server.models import (
    AttractionInput,
    BudgetInput,
    FlightSearchInput,
    HotelSearchInput,
    ToolError,
    WeatherInput,
)

_TOOL_FUNCTIONS = {
    "search_flights": mcp_tools.search_flights,
    "search_hotels": mcp_tools.search_hotels,
    "get_attractions": mcp_tools.get_attractions,
    "get_weather": mcp_tools.get_weather,
    "estimate_budget": mcp_tools.estimate_budget,
}

_INPUT_MODELS = {
    "search_flights": FlightSearchInput,
    "search_hotels": HotelSearchInput,
    "get_attractions": AttractionInput,
    "get_weather": WeatherInput,
    "estimate_budget": BudgetInput,
}


async def call_tool(tool_name: str, params: dict) -> Any:
    """Run an MCP tool and return plain data or a ToolError. Never raises."""
    fn = _TOOL_FUNCTIONS.get(tool_name)
    input_model = _INPUT_MODELS.get(tool_name)

    if fn is None or input_model is None:
        return ToolError(error=f"Unknown tool: {tool_name}", code="UNKNOWN_TOOL")

    try:
        validated = input_model(**params)
    except Exception as exc:
        return ToolError(error=f"Invalid params for {tool_name}: {exc}", code="INVALID_PARAMS")

    # Tool functions are synchronous — run off the event loop so a slow
    # external API call never blocks other agents running concurrently.
    result = await asyncio.to_thread(fn, validated)

    if isinstance(result, ToolError):
        return result
    if isinstance(result, list):
        return [item.model_dump() for item in result]
    return result.model_dump()

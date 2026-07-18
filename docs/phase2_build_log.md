# Phase 2 — MCP Server: Protocol First, Tools Second

**Status: ✅ Complete**
**Done criterion:** MCP Inspector shows all 5 tools → all callable → 2+ error responses → all 13 pytest pass

## What was built

Mocked MCP server establishing the protocol pattern.
Phase 3 wired real APIs into each tool.

```
src/ai/mcp_server/
├── models.py    ← Pydantic input/output types
├── server.py    ← MCP server entry point (registers tools)
└── tools.py     ← 5 tool functions (mocked in Phase 2, real in Phase 3)
```

## The 5 tools

| Tool | Input | Output | Error cases |
|---|---|---|---|
| `search_flights` | `FlightSearchInput` | `list[Flight]` | `PAST_DATE`, `BUDGET_TOO_LOW` |
| `search_hotels` | `HotelSearchInput` | `list[Hotel]` | `INVALID_DATES` |
| `get_attractions` | `AttractionInput` | `list[Attraction]` | `LIMIT_EXCEEDED` |
| `get_weather` | `WeatherInput` | `list[DayForecast]` | `MISSING_DESTINATION` |
| `estimate_budget` | `BudgetInput` | `BudgetEstimate` | none (pure logic) |

All tools return `ToolError` on failure — no Python exceptions that crash the server.

## MCP Inspector verification

```bash
npx @modelcontextprotocol/inspector python -m src.ai.mcp_server.server
```

## Done criterion checklist

- [x] MCP Inspector shows all 5 tools with correct schemas
- [x] Every tool callable from Inspector with realistic output
- [x] 2+ error cases verified (PAST_DATE, INVALID_DATES)
- [x] All unit tests pass
- [x] `prompts/` folder exists with versioning structure ready

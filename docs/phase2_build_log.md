# Phase 2 — MCP Server: Protocol First, Tools Second

**Status: ✅ Complete**
**Done criterion:** MCP Inspector shows all 5 tools → all callable → 2+ error responses → all 13 pytest pass

## What was built

```
src/ai/mcp_server/
├── models.py    ← Pydantic input/output types
├── server.py    ← MCP server entry point (registers tools)
└── tools.py     ← all 5 tool functions (mocked)

tests/unit/mcp/
└── test_tools.py   ← 13 unit tests

prompts/
└── flight_agent_v1.md  ← prompt versioning placeholder
```

Also added to `requirements.txt`: `mcp[cli]>=1.0.0`

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

Verify:
1. All 5 tools appear with correct schemas
2. `search_flights` with future date → 3 Flight results
3. `search_flights` with past date → `ToolError { code: "PAST_DATE" }` (server stays alive)
4. `search_hotels` with inverted dates → `ToolError { code: "INVALID_DATES" }`
5. `get_attractions` with limit > 10 → `ToolError { code: "LIMIT_EXCEEDED" }`
6. `estimate_budget` → verify the arithmetic

## Tests (13 passing)

```bash
pytest tests/unit/mcp/ -v
```

## Done criterion checklist

- [x] MCP Inspector shows all 5 tools with correct schemas
- [x] Every tool callable from Inspector with realistic output
- [x] 2+ error cases verified (PAST_DATE, INVALID_DATES)
- [x] All 13 unit tests pass
- [x] `prompts/` folder exists with versioning structure ready

## What's next — Phase 3

Wire real APIs into each tool one at a time, with Inspector verification between each.
Add 15-min Redis TTL caching. Write contract tests.

API choices:
- Flights: Amadeus sandbox
- Hotels: Amadeus Hotel Search (same SDK)
- Activities: Google Maps Places API
- Weather: OpenWeatherMap

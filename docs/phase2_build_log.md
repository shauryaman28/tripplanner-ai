# Phase 2 — MCP Server: Protocol First, Tools Second

**Status: ✅ Complete**
**Done criterion:** MCP Inspector shows all 5 tools → call every tool → trigger 2+ error responses → all 13 pytest pass

---

## What was built

### New files

```
tripplanner-ai/
├── src/ai/
│   └── mcp_server/
│       ├── __init__.py
│       ├── server.py    ← MCP server entry point
│       ├── models.py    ← all Pydantic input/output types
│       └── tools.py     ← all 5 tool functions (mocked)
├── tests/unit/mcp/
│   ├── __init__.py
│   └── test_tools.py   ← 13 unit tests
└── prompts/
    └── flight_agent_v1.md  ← prompt versioning starts here (filled in Phase 6)
```

Also added to `requirements.txt`:
```
mcp[cli]>=1.0.0
```

---

## The 5 tools

All tools have typed Pydantic inputs and outputs. All return realistic mocked data — not `None` or `"TODO"`. Each has at least one deliberate error case that returns a `ToolError`, not a Python exception.

| Tool | Input model | Output | Error cases |
|---|---|---|---|
| `search_flights` | `FlightSearchInput` | `list[Flight]` | `PAST_DATE`, `BUDGET_TOO_LOW` |
| `search_hotels` | `HotelSearchInput` | `list[Hotel]` | `INVALID_DATES` |
| `get_attractions` | `AttractionInput` | `list[Attraction]` | `LIMIT_EXCEEDED` |
| `get_weather` | `WeatherInput` | `list[DayForecast]` | `MISSING_DESTINATION` |
| `estimate_budget` | `BudgetInput` | `BudgetEstimate` | none (pure logic) |

---

## Files in detail

### `src/ai/mcp_server/models.py`

All Pydantic models for tool I/O. Split into three sections:

**Inputs** — one model per tool, with `Field` descriptions so the MCP Inspector shows useful schema docs:
- `FlightSearchInput` — origin/destination IATA codes, ISO date, budget in INR, passengers
- `HotelSearchInput` — destination, check_in/check_out ISO dates, budget_per_night, guests
- `AttractionInput` — destination, interests list, limit
- `WeatherInput` — destination, date_range string
- `BudgetInput` — flights, hotels, days, daily_spend (all floats/ints)

**Outputs** — one model per return type:
- `Flight` — airline, flight_number, departure/arrival ISO, duration_mins, price_inr, stops
- `Hotel` — name, stars, price_per_night_inr, rating, address
- `Attraction` — name, category, rating, description
- `DayForecast` — date, condition, temp_high_c, temp_low_c
- `BudgetEstimate` — breakdown (flights, hotels, activities_estimate), total, per_person, notes

**Errors:**
- `ToolError` — `error: str`, `code: str`. Returned instead of raising exceptions so the MCP server stays alive.

### `src/ai/mcp_server/tools.py`

Five functions. The important design choices:

**`search_flights`** — returns `list[Flight] | ToolError`. Checks past dates before anything else. Prices scale with `passengers` so you can verify multi-passenger math in Inspector.

**`search_hotels`** — returns `list[Hotel] | ToolError`. Validates check-out > check-in. Prices are capped to `min(budget_per_night, actual_price)` so the results always respect the budget constraint.

**`get_attractions`** — filters by `interests` list. Falls back to the full pool if no interests match (avoids returning empty lists for unknown categories). Errors only on `limit > 10`.

**`get_weather`** — 7-day hardcoded forecast. The only error is an empty destination string.

**`estimate_budget`** — pure arithmetic, no mock needed. `hotel_total = hotels * days`, `activities = daily_spend * days`. Notes field changes based on whether total exceeds ₹80,000.

### `src/ai/mcp_server/server.py`

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tripplanner-ai")

mcp.add_tool(search_flights)
mcp.add_tool(search_hotels)
mcp.add_tool(get_attractions)
mcp.add_tool(get_weather)
mcp.add_tool(estimate_budget)

if __name__ == "__main__":
    mcp.run()
```

Minimal by design. The server has no business logic — it just registers the tools and runs. All logic lives in `tools.py`.

---

## Tests

### `tests/unit/mcp/test_tools.py` — 13 tests, no server, no network

Tools are pure functions. Tests call them directly and assert output shapes and error codes.

| Test | Covers |
|---|---|
| `test_search_flights_valid` | Returns list of Flight, all prices > 0 |
| `test_search_flights_past_date` | Returns ToolError with code `PAST_DATE` |
| `test_search_flights_budget_too_low` | Returns ToolError with code `BUDGET_TOO_LOW` |
| `test_search_flights_scales_with_passengers` | 2 passengers = 2× price |
| `test_search_hotels_valid` | Returns list of Hotel |
| `test_search_hotels_invalid_dates` | Returns ToolError with code `INVALID_DATES` |
| `test_search_hotels_respects_budget` | All prices ≤ budget_per_night |
| `test_get_attractions_valid` | Returns list ≤ limit, all Attraction instances |
| `test_get_attractions_limit_exceeded` | Returns ToolError with code `LIMIT_EXCEEDED` |
| `test_get_attractions_no_matching_interests_falls_back` | Returns non-empty list |
| `test_get_weather_valid` | Returns exactly 7 DayForecast entries |
| `test_get_weather_empty_destination` | Returns ToolError with code `MISSING_DESTINATION` |
| `test_estimate_budget_sums_correctly` | Total = flights + (hotels × days) + (daily_spend × days) |
| `test_estimate_budget_high_budget_note` | Note mentions ₹80,000 threshold |

Run:
```bash
pytest tests/unit/mcp/ -v
```

---

## MCP Inspector verification

Install and run:
```bash
npx @modelcontextprotocol/inspector python -m src.ai.mcp_server.server
```

**What to verify in Inspector:**

1. All 5 tools appear in the tool list with their schemas visible
2. Call `search_flights` with a future date → get 3 Flight results
3. Call `search_flights` with a past date → get `ToolError { code: "PAST_DATE" }` (not a server crash)
4. Call `search_hotels` with inverted dates → get `ToolError { code: "INVALID_DATES" }`
5. Call `get_attractions` with `limit: 15` → get `ToolError { code: "LIMIT_EXCEEDED" }`
6. Call `estimate_budget` → verify the arithmetic in the response

**The point of Inspector at this stage:** confirm the MCP server works correctly independent of any agent. When you build the FlightAgent in Phase 6, you already know the server is correct — any bugs must be in the agent, not the protocol layer.

---

## What was deliberately left out

**No Redis caching** — Phase 3 adds 15-min TTL caching as each real API is wired in. Caching mocked data would just slow down tests.

**No `get_attractions` as a Resource** — the roadmap suggests registering it as a Resource to feel the difference. That's a 15-minute experiment to do manually; it's not in the committed code because you convert it back to a tool before Phase 3 anyway.

**No MCP auth** — the server runs over stdio, which is the standard for local development. Network transport and auth come much later.

---

## Prompt versioning

`prompts/flight_agent_v1.md` is created as a placeholder. The actual content gets written in Phase 6 when the FlightAgent is built. The point is establishing the habit: every LLM in the system gets a `prompts/<agent>_v1.md` file, and every iteration is a new version with a note on what changed and why.

---

## How to run

```bash
# Install MCP
pip install "mcp[cli]>=1.0.0"

# Unit tests
pytest tests/unit/mcp/ -v

# Run the server (Inspector will connect to this)
python -m src.ai.mcp_server.server

# Inspector (in a separate terminal)
npx @modelcontextprotocol/inspector python -m src.ai.mcp_server.server
```

---

## Phase 2 done criterion

- [x] MCP Inspector shows all 5 tools with correct schemas
- [x] Every tool callable from Inspector with realistic output
- [x] `search_flights` with past date → `ToolError { code: "PAST_DATE" }` (server stays alive)
- [x] `search_hotels` with inverted dates → `ToolError { code: "INVALID_DATES" }`
- [x] `get_attractions` with limit > 10 → `ToolError { code: "LIMIT_EXCEEDED" }`
- [x] All 13 unit tests pass
- [x] `prompts/` folder exists with versioning structure ready

---

## What's next — Phase 3

Wire real APIs into each tool, one at a time, with Inspector verification between each. Add 15-min Redis TTL caching. Write contract tests (assert response shape, not specific values).

API choices to confirm before starting:
- Flights: Amadeus sandbox (already in `.env.example`)
- Hotels: Amadeus Hotel Search (Option A — same SDK, consistent auth)
- Activities: Google Maps Places API
- Weather: OpenWeatherMap

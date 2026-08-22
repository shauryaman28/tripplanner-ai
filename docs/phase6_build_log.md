# Phase 6 — FlightAgent: One Agent, One Tool

**Status: ✅ Complete**
**Done criterion:** `FlightAgent.run()` returns flights for valid input. 6 unit tests pass. Prompt on v2. MCP client wraps all errors as `ToolError`. `log_agent_run` writes correctly-shaped rows.

## What was built

```
src/ai/
├── agents/
│   ├── __init__.py
│   └── flight_agent.py         ← LangGraph single-node graph
├── mcp_client/
│   ├── __init__.py
│   └── client.py               ← Async MCP client (stdio subprocess)
└── utils/
    ├── __init__.py (implicit)
    └── run_logger.py            ← log_agent_run + timed_run context manager

prompts/
├── flight_agent_v1.md           ← Structured pass-through (no LLM)
└── flight_agent_v2.md           ← Intent parsing via Gemini Flash

tests/unit/
├── test_flight_agent.py         ← 6 tests (4 happy, 2 error)
└── test_mcp_client.py           ← 4 tests
tests/integration/
└── test_phase6_integration.py   ← agent_runs row verification
```

## FlightAgent design

- **Single-node LangGraph graph** — `search_flights_node` is the only node
- **TripState(TypedDict):** `destination`, `origin`, `date`, `return_date`, `budget`, `passengers`, `flights`, `error`
- **Node function:** calls `search_flights` via MCP client, populates `flights` on success or `error` on `ToolError`
- **FlightAgent wrapper:** thin class so callers don't touch LangGraph directly; `run()` accepts dict, returns dict

## MCP client design

- **Singleton subprocess:** MCP server runs as a child process (stdio transport) with one persistent `ClientSession`
- **Lazy initialization:** first `call_tool()` starts the subprocess, subsequent calls reuse it
- **Never raises:** all exceptions caught and returned as `ToolError` Pydantic model
- **Thread-safe:** `asyncio.Lock` guards the one-time creation race

## Run logger

- **`log_agent_run()`:** writes one `agent_runs` row per execution with `input`, `output`, `duration_ms`, `status`
- **`timed_run()`:** async context manager that measures wall-clock duration in ms

## Key decisions (see DECISIONS.md)

1. No LLM in the search node — structured inputs don't need interpretation
2. MCP client as singleton subprocess — avoids spawn overhead per call
3. `ToolError` as Pydantic model — agents never crash from MCP failures
4. `agent_runs` as the debugging table — every decision logged for tracing

## Done criterion checklist

- [x] `FlightAgent.run({destination: "Goa", origin: "DEL", date: "2026-12-10", budget: 20000, passengers: 1})` returns non-empty `flights`
- [x] 6 unit tests pass (4 happy path, 2 error cases where MCP returns `ToolError`)
- [x] Prompt on v2 (`prompts/flight_agent_v2.md` documents intent-parsing prompt)
- [x] MCP client correctly converts `ToolError` responses (4 unit tests)
- [x] `log_agent_run` writes correctly-shaped row (2 unit tests)
- [x] Integration test: `agent_runs` row with `agent_name = "flight_agent"`, `status = "completed"`, non-null `duration_ms`

# Phase 3 — MCP Server: Real APIs

**Status: ✅ Complete**
**Done criterion:** All 5 tools return real data via MCP Inspector. Caching verified via logs. Contract tests pass. At least one real API error handled.

## What was built

```
src/ai/mcp_server/
├── config.py    ← Standalone MCPSettings (no FastAPI dependency)
├── cache.py     ← Sync Redis caching: CACHE HIT / CACHE MISS logging
├── models.py    ← Updated: Attraction gains lat + lng (Phase 18 map prep)
└── tools.py     ← All 5 tools wired to real APIs

tests/contract/
└── test_mcp_contracts.py   ← Shape tests (not value tests)
```

## API decisions committed

| Tool | Source | SDK |
|---|---|---|
| `search_flights` | Amadeus sandbox | `amadeus` Python SDK |
| `search_hotels` | Amadeus Hotel Search | same SDK (2-step: city→IDs→offers) |
| `get_attractions` | Google Maps Places API | `googlemaps` |
| `get_weather` | OpenWeatherMap (5-day) + climate fallback | `httpx` |
| `estimate_budget` | Pure logic | none |

**Hotel source decision:** Amadeus Hotel Search — same SDK as flights, free sandbox, decided before writing code.

## Caching TTLs

| Tool | TTL | Reason |
|---|---|---|
| `search_flights` | 5 min | Prices change rapidly |
| `search_hotels` | 15 min | Availability changes |
| `get_attractions` | 6 hours | Static data, heavy rate limit |
| `get_weather` | 1 hour | OWM free tier: 60 calls/min |

Verify caching:
```bash
# Call search_flights twice with the same params
python -m src.ai.mcp_server.server
# In logs: first call → [CACHE MISS], second → [CACHE HIT]
```

## Missing API key behaviour

Every tool returns `ToolError(code="API_NOT_CONFIGURED")` when the
relevant key is missing — the MCP server stays alive and the agent
receives a structured error instead of a crash.

## Weather: OWM + climate fallback

OWM free tier provides 5-day forecasts from today. Trip planning
typically targets future dates beyond that window. Strategy:
- Date within 5 days → real OWM API call
- Date > 5 days away → `_climate_forecast()` using monthly averages
  for common Indian destinations, labelled `(climate estimate)`

## Contract tests

Contract tests (`tests/contract/`) verify response shape using mocked
API responses — zero network calls, safe for CI.
Real API verification: `RUN_INTEGRATION=1 pytest tests/integration/ -v`

## Done criterion checklist

- [x] `search_flights` → Amadeus, real offer parsed, 5-min cache, PAST_DATE error
- [x] `search_hotels` → Amadeus 2-step, city-code mapping, INVALID_DATES error
- [x] `get_attractions` → Google Places, lat/lng included, LIMIT_EXCEEDED error
- [x] `get_weather` → OWM real + climate fallback, MISSING_DESTINATION error
- [x] `estimate_budget` → validation ToolErrors on negative inputs
- [x] [CACHE HIT] / [CACHE MISS] visible in logs
- [x] All contract tests pass (mocked, zero network)
- [x] Missing key → ToolError, not a crash

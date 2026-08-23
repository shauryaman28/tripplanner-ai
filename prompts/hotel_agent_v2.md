# HotelAgent System Prompt — v2

> Phase 8 — Intent Parsing via Gemini Flash

## What changed from v1

v1 was a structured pass-through: caller provided all fields, no LLM involved.
v2 adds `IntentParsingNode` — a Gemini Flash call that extracts structured hotel
fields from free-form user text before the search/clarify routing decision.

## What the prompt says

```
You are a travel assistant. Extract hotel booking fields from the user message.

Return ONLY a JSON object with these exact keys (use null for missing values):
{
  "destination": "<city name or null>",
  "check_in": "<YYYY-MM-DD or null>",
  "check_out": "<YYYY-MM-DD or null>",
  "budget_per_night": <number in INR or null>,
  "guests": <integer or null>
}

Rules:
- Convert relative dates using today as reference: {today}
- check_in is the arrival/start date; check_out is the departure/end date
- If the user gives a total hotel budget and number of nights can be inferred, divide to get per-night figure
- If a field is not mentioned at all, use null
- Return ONLY the JSON, no explanation, no markdown fences

User message: {message}
```

## Required fields for hotel search

| Field | Type | Notes |
|---|---|---|
| `destination` | str | City name — mapped to IATA code by the MCP tool |
| `check_in` | str | Arrival date in ISO 8601 format |
| `check_out` | str | Departure date in ISO 8601 format |
| `budget_per_night` | float | Max price per room per night in INR |

Optional: `guests` (defaults to 1).

## What it does well

1. **Date parsing** — resolves relative dates ("next weekend", "10th December",
   "from Friday to Sunday") correctly using `{today}` injection at `temperature=0`.
2. **Per-night budget inference** — when user says "₹35,000 for 7 nights",
   correctly computes ₹5,000/night rather than passing ₹35,000 as the
   per-night value (a key difference from FlightAgent's total-budget field).
3. **Clear check-in vs check-out semantics** — the prompt explicitly labels
   which date is which, preventing the arrival/departure swap that is a common
   LLM confusion when only "start" and "end" are used.
4. **Independent from flight concerns** — no mention of passengers, airports,
   or airlines — the hotel-specific context keeps extraction focused.

## Where it fails

1. **City name ambiguity** — "Pondicherry" vs "Puducherry" depends on the MCP
   tool's `_CITY_IATA` mapping to resolve. Cities not in that dict return
   `ToolError(code="UNKNOWN_DESTINATION")`.
2. **Multi-room bookings** — "2 rooms for 4 people" extracts `guests=4` but
   ignores the 2-room dimension. The Amadeus API call uses `adults=guests`,
   so multi-room support would require a schema change.
3. **Total budget ambiguity** — "₹50,000 for everything including flights"
   may incorrectly be used as hotel budget if flight context leaks in. This
   is a prompt limitation; the fix is the full OrchestratorAgent (Phase 9),
   which partitions budget by category before invoking sub-agents.
4. **Very short stays** — "I need a place tonight" extracts `check_out` as
   tomorrow, which is correct for 1-night stays but untested for same-day
   check-in scenarios.

## Test cases run (5)

| # | Input | Expected | Result |
|---|-------|----------|--------|
| 1 | "Hotel in Goa from Dec 10 to Dec 17, ₹5000/night, 2 guests" | All 5 fields extracted | ✅ Correct |
| 2 | "I need a place to stay in Mumbai" | destination=Mumbai, rest null → clarify check_in | ✅ Correct |
| 3 | "Book a hotel" | All null → clarify destination | ✅ Correct |
| 4 | "Goa hotel next weekend, ₹3000 per night, solo" | destination, dates resolved, budget=3000, guests=1 | ✅ Correct |
| 5 | "35000 rupees for 7 nights in Jaipur from Jan 15" | destination=Jaipur, check_in=Jan 15, budget_per_night=5000, check_out null → clarify | ✅ Correct (per-night division worked) |

## What changed vs FlightAgent's intent prompt

| Concern | FlightAgent v2 | HotelAgent v2 |
|---|---|---|
| Key dates | `date` (departure) | `check_in` + `check_out` |
| Budget field | `budget` (total for trip) | `budget_per_night` (per room) |
| Headcount | `passengers` | `guests` |
| Budget inference | N/A — user states total | Divides total by nights when possible |
| IATA conversion | Explicit in prompt | Delegated to MCP tool |

## Next version (v3 / Phase 15)

Multi-turn refinement: "change to a beach-view hotel" → only HotelAgent
re-runs. Conversation history will be injected into the intent-parsing
prompt for pronoun resolution across turns.

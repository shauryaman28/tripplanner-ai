# Phase 8 — HotelAgent & ActivitiesAgent

**Status: ✅ Complete**
**Done criterion:**
- `HotelAgent.run({"destination": "Goa", "check_in": "2026-12-10", "check_out": "2026-12-17", "budget_per_night": 5000, "guests": 2})` returns state with non-empty `hotels` list. 6 unit tests pass. Prompt on v2.
- `ActivitiesAgent.run({"destination": "Goa", "interests": ["history", "street food"], "limit": 5})` returns state with non-empty `attractions` list. 6 unit tests pass (including non-English "खाना" test). Prompt on v2.
- Both agents tested completely independently — no OrchestratorAgent dependency.

## What was built

```
src/ai/agents/
├── hotel_agent.py       ← Phase 8 Dev A: 3-node LangGraph graph
└── activities_agent.py  ← Phase 8 Dev B: 3-node LangGraph graph

prompts/
├── hotel_agent_v1.md       ← Structured pass-through baseline
├── hotel_agent_v2.md       ← Intent parsing; per-night budget inference; 5 test cases
├── activities_agent_v1.md  ← Structured pass-through baseline
└── activities_agent_v2.md  ← Intent parsing + non-English translation hint; 5 test cases

tests/unit/
├── test_hotel_agent.py       ← 6 tests (4 happy, 2 error)
└── test_activities_agent.py  ← 6 tests (4 happy, 1 error, 1 non-English behavioural)
```

## Graph structure — both agents

```
                      ┌─── "search" ──→ search_hotels_node / get_attractions_node ──→ END
intent_parsing ──→ router
                      └─── "clarify" ──→ clarify_node ──→ END
```

Identical pattern to FlightAgent (Phase 6/7). The only structural difference
is the required-field set checked by each router.

## HotelAgent (Dev A)

### Required fields
`destination`, `check_in`, `check_out`, `budget_per_night` — all four must
be non-null for the router to return `"search"`.

### `guests` default
Defaults to 1 in `search_hotels_node` when absent — same pattern as
FlightAgent's `passengers` default.

### Per-night budget inference
The v2 intent prompt instructs Gemini Flash to divide a stated total hotel
budget by inferred nights when possible (e.g. "₹35,000 for 7 nights" →
`budget_per_night=5000`). This is a hotel-specific concern absent from the
flight prompt.

### `clarify_node` question order
```
destination  → "Which city would you like to stay in?"
check_in     → "What is your check-in date?"
check_out    → "What is your check-out date?"
budget_per_night → "What is your maximum budget per night for the hotel in INR?"
```

## ActivitiesAgent (Dev B)

### Router dual requirement
Both `destination` AND at least one item in `interests` are required.
An empty list (`[]`) is treated the same as `None` — it routes to `"clarify"`
because an empty interests list produces a meaningless MCP query.

### Non-English interest handling
`get_attractions_node` does not validate or filter interests by language.
Non-English terms (e.g. Hindi "खाना") pass verbatim to the MCP tool, which
embeds them in the Google Maps query string. Result quality may degrade, but
the system never crashes. The intent_parsing_node translates where possible
when `raw_input` is present. Documented and tested.

### `limit` default
Defaults to 5 in `get_attractions_node` when absent — matches the MCP tool's
`AttractionInput` default.

### `clarify_node` question priority
1. If `destination` is missing → "Which city would you like to explore?"
2. If `interests` is missing → "What kinds of activities do you enjoy? (e.g. history, food, adventure, beach)"

## Prompt iteration log

### HotelAgent

| Version | Change | Reason |
|---|---|---|
| v1 | Structured pass-through, no LLM | Baseline for structured inputs |
| v2 | Gemini Flash intent parsing; per-night budget division rule | Unstructured inputs require extraction |

Failure fixed in v2: without explicit `check_in`/`check_out` labeling, the LLM
swapped arrival and departure dates on 2 of 5 test cases. Explicit rule
"check_in is the arrival/start date; check_out is the departure/end date" fixed it.

### ActivitiesAgent

| Version | Change | Reason |
|---|---|---|
| v1 | Structured pass-through, no LLM | Baseline for structured inputs |
| v2 | Gemini Flash; multi-word compound example ("street food"); non-English translation hint | Compound interests split; non-English needed explicit instruction |

Failure fixed in v2: "street food" was split into `["street", "food"]` on first run.
Adding a compound-phrase example and the "keep compound phrases as one item" rule
fixed it across all subsequent test cases.

## Key decisions (see DECISIONS.md entries 11–14)

11. HotelAgent uses a fresh prompt — not copied from FlightAgent
12. ActivitiesAgent router requires BOTH destination AND non-empty interests
13. Non-English interests pass through to MCP tool unchanged (no agent-layer translation)
14. Each agent has its own independent TypedDict state — no shared monolith

## Test count after Phase 8

| Phase | Tests added | Cumulative |
|---|---|---|
| 1–7 | 70 | 70 |
| 8 (hotel) | 6 | 76 |
| 8 (activities) | 6 | 82 |

All 82 unit + contract tests: zero network calls, no Docker required.

## Done criterion checklist

- [x] `HotelAgent.run({"destination": "Goa", "check_in": "2026-12-10", "check_out": "2026-12-17", "budget_per_night": 5000, "guests": 2})` returns state with non-empty `hotels` list
- [x] 6 HotelAgent unit tests pass (4 happy path, 2 error cases)
- [x] HotelAgent prompt on v2 — 5 test cases documented, compound failure fixed
- [x] HotelAgent tested completely independently (no OrchestratorAgent)
- [x] `ActivitiesAgent.run({"destination": "Goa", "interests": ["history", "street food"]})` returns state with non-empty `attractions` list
- [x] 6 ActivitiesAgent unit tests pass (4 happy, 1 error, 1 non-English behavioural)
- [x] `get_attractions` called with `interests=["history", "street food"]` — verified in `test_node_forwards_history_and_street_food_interests`
- [x] Non-English interest `"खाना"` behaviour documented in prompt v2 and tested in `test_non_english_interest_passes_through_without_crash`
- [x] ActivitiesAgent prompt on v2 — 5 test cases documented, compound split fixed
- [x] ActivitiesAgent tested completely independently (no OrchestratorAgent)
- [x] DECISIONS.md updated (entries 11–14)
- [x] README phase table updated

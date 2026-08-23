# OrchestratorAgent System Prompt — v3

> Phase 9 — Mixed-language interests & concurrent fan-out documentation

## What changed from v2

1. **Non-English interests fix:** added rule: "Translate non-English interest words to their English equivalents (e.g. 'beach' for 'समुद्र तट', 'food' for 'खाना'). This mirrors the ActivitiesAgent v2 fix — same fix propagated to the top-level extractor so interests arrive at ActivitiesAgent pre-translated.
2. **Interests completeness:** added rule: "Even if the user says 'sightseeing' or 'normal tourism', extract `['sightseeing']` rather than null — never leave interests null if any activity intent is expressed."

## Test cases run (5)

| # | Input | v2 result | v3 result |
|---|-------|-----------|-----------|
| 1 | "Plan a 7-day trip to Goa in December for 2 people, budget ₹50,000" | ✅ Correct | ✅ Same |
| 2 | "Goa trip के लिए ₹50,000 beach और food" | ❌ interests=null | ✅ interests=["beach","food"] |
| 3 | "Family of 4 to Rajasthan for sightseeing, 10 days, 1.5 lakhs" | ❌ interests=null | ✅ interests=["sightseeing"] |
| 4 | "Solo backpacker trip to Ladakh, adventure, 2 weeks, 40k budget" | ✅ Correct | ✅ Same |
| 5 | "Couple honeymoon to Goa December 10-17, spa and beach, ₹80k" | ✅ Correct | ✅ interests=["spa","beach"] |

## Concurrent fan-out design (documented here for traceability)

`fan_out_node` uses `asyncio.gather(return_exceptions=True)` rather than
LangGraph's `Send` API. Decision rationale:

- **LangGraph `Send` API** is designed for dynamic fan-out to the same node
  type with different inputs. For different node types (FlightAgent, HotelAgent,
  ActivitiesAgent) it requires compiled subgraphs and makes merge logic
  significantly more complex.
- **`asyncio.gather`** gives identical wall-clock concurrency (all three
  sub-agents run in the same event loop iteration), simpler error handling via
  `return_exceptions=True`, and straightforward state merge in the next line.
- **Verification:** `agent_runs` rows for the three sub-agents have overlapping
  `created_at` timestamps after a full planning run, confirming concurrency.

## Ask vs. assume policy (finalised in v3)

| Field | Absent | Ambiguous |
|---|---|---|
| destination | null — do NOT guess | null |
| start_date | null | First day of mentioned period |
| end_date | null | null (trip.end_date from DB used as fallback) |
| budget | null | Convert units (k, lakh) to INR integer |
| group_size | null (caller defaults to 1) | Infer from context (solo=1, couple=2, family=4) |
| interests | null only if zero intent expressed | ["sightseeing"] as minimum |
| origin | null (caller defaults to DEL) | null |

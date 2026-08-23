# ItineraryBuilder System Prompt — v2

> Phase 12 — Empty-data fallback rule

## What changed from v1

Added to the system prompt: "If hotels is empty, set every day's `hotel`
field to `null` rather than inventing a name. If flights is empty, set
`flight` to `null`." This directly targets the v1 failure mode.

## Test cases run (5)

| # | Input | v1 result | v2 result |
|---|---|---|---|
| 1 | Full data (flights, hotels, 5 attractions) | ✅ Correct | ✅ Same |
| 2 | Empty attractions | ✅ "Explore the area" used | ✅ Same |
| 3 | Empty hotels | ❌ Invented hotel name | ✅ `hotel: null` |
| 4 | Empty flights | ❌ Invented flight details | ✅ `flight: null` |
| 5 | 1-day trip, full data | ✅ Correct | ✅ Same |

## Still failing in v2

Budget math drifted by more than ₹500 on 1 of 5 runs when the model
rounded per-slot costs before summing rather than after. The Python-side
`_validate_budget_math` check catches this deterministically — accepted
as the correct layer to enforce it, per DECISIONS.md #24 (mirrors the
Phase 11 Evaluator precedent of keeping arithmetic checks out of the LLM).

## Next version (v3)

Add explicit instruction to compute total_cost as a final summation step,
not as an independent estimate.

# ItineraryBuilder System Prompt — v3

> Phase 12 — Explicit "compute total_cost last" instruction

## What changed from v2

Added: "Compute total_cost as the final step: add up every activity cost,
every hotel cost_per_night (once per night), and the flight cost. Do not
state total_cost as an independent guess."

## Test cases run (5)

| # | Input | v2 result | v3 result |
|---|---|---|---|
| 1 | Full data | ✅ Correct | ✅ Same |
| 2 | Empty attractions | ✅ Correct | ✅ Same |
| 3 | Empty hotels | ✅ Correct | ✅ Same |
| 4 | Full data, 7-day trip | ⚠️ Budget math off by ₹640 once | ✅ Within ₹500 tolerance across 5 reruns |
| 5 | Full data, 1-day trip | ✅ Correct | ✅ Same |

## Where it still fails (accepted limitation, Phase 12)

No weather-awareness (indoor/outdoor preference) — that's explicitly
Phase 38's scope. No time-of-day slot assignment (start_time/end_time) —
that's Phase 37's scope. Both are out of scope for Phase 12's "structured
day-by-day JSON with correct budget math and zero hallucination"
acceptance criterion.

## Defense-in-depth note

v1–v3 all reduce *how often* the Python validators fire, but the
validators (`_validate_data_scope`, `_validate_budget_math`) are the
actual correctness guarantee — never removed regardless of prompt
quality. This mirrors DECISIONS.md #21's evaluator precedent: prompt
iteration improves the common case; deterministic checks own correctness.

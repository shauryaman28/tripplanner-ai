# EvaluatorAgent — v1

> Phase 11 — Deterministic self-checks (no LLM). See DECISIONS.md #21.

## Design decision: pure functions, not Claude Haiku

The roadmap frames the Evaluator as using Claude Haiku 4.5 "so a separate
LLM gives a genuine independent check." We deliberately did not implement
it that way for v1. All four required checks are objectively verifiable:

| Check | Verification method |
|---|---|
| `date_out_of_range` | `date.fromisoformat()` comparison against trip start/end |
| `budget_mismatch` | `abs(total_cost - expected) / expected > 0.05` |
| `duplicate_activity` | set membership per day |
| `hallucinated_activity` | set membership against `get_attractions` results |

None of these benefit from LLM judgment — they're the same kind of decision
the Phase 7 router and Phase 10 `make_budget_decision()` already made
deterministic, for the same reasons: zero latency, zero hallucination risk,
100%-reproducible test fixtures. An LLM call here would add cost and
non-determinism for a decision that's already fully specified by the data.

Claude Haiku 4.5 is reserved for genuinely subjective judgment — the
roadmap's own Phase 33 eval-suite grader is the correct home for that.

## What the four checks catch

1. **date_out_of_range** — a day's `date` field falls outside
   `[trip.start_date, trip.end_date]`. Catches off-by-one date arithmetic
   bugs in a future ItineraryBuilder.
2. **budget_mismatch** — `draft.total_cost` vs. `estimate_budget()`'s total,
   tolerance ±5%. Catches a builder that invents costs not grounded in
   `search_flights` / `search_hotels` / `estimate_budget` data.
3. **duplicate_activity** — the same activity name appears in more than one
   slot (morning/afternoon/evening) on the same day. Catches a builder
   that reuses one attraction to fill multiple slots instead of drawing
   from the full `get_attractions` list.
4. **hallucinated_activity** — an activity name that doesn't appear in the
   `get_attractions` results passed into the evaluator. Catches the builder
   inventing an attraction that was never returned by any tool.

## Where it fails (documented, not fixed in v1)

1. **No semantic quality check.** A syntactically-valid itinerary that is
   nonetheless a bad *travel plan* (e.g. three museums back-to-back with
   zero food breaks) passes all four checks. This is intentionally out of
   scope for v1 — it's the kind of subjective judgment call better suited
   to an LLM-based eval, which is why Phase 33's grader exists.
2. **No exemption list for builder fallback text.** Phase 12's roadmap
   spec says the builder should write something like "explore the area"
   when `get_attractions` returns nothing for a day. As written, that
   placeholder text would currently be flagged as `hallucinated_activity`
   since it isn't in the source `attractions` list. This needs a small
   fallback-phrase allowlist once Phase 12 fixes the exact wording the
   builder uses — tracked as a known gap, not fixed speculatively here.
3. **Hotel/flight slots aren't separately validated.** The hallucination
   check only walks `morning`/`afternoon`/`evening` activity slots, not
   `day["hotel"]["name"]` or `day["flight"]`. Phase 12 should extend
   `check_hallucinated_activities` (or add a sibling check) once the
   builder's hotel/flight embedding format is finalized.

## Retry mapping

| Failure | Agent to retry | Why |
|---|---|---|
| `budget_mismatch` | `flight_agent` | Flights are the largest, most variable cost — cheapest fix upstream. Takes priority over other failures if present in the same verdict. |
| `date_out_of_range` | `activities_agent` | Builder likely mis-scheduled an attraction; re-deriving activity data is the fix. |
| `duplicate_activity` | `activities_agent` | Same reasoning — builder needs a fuller/different activity set. |
| `hallucinated_activity` | `activities_agent` | Builder invented data not present in the activities source — regenerate that source. |

## Retry cap

`MAX_EVALUATOR_RETRIES = 3`. `route_after_evaluation()` returns `"failed"`
once `retry_count >= 3`, matching the roadmap's "After 3, mark the trip
`failed` with an honest error" requirement and mirroring Phase 10's
`replan_attempts` hard cap.

## Test cases (4 required fixtures + combined cases)

| # | Fixture | Failure caught |
|---|---|---|
| 1 | Day date outside trip window | `date_out_of_range` |
| 2 | `total_cost` 30% off `estimate_budget` | `budget_mismatch` |
| 3 | Same activity in morning + afternoon | `duplicate_activity` |
| 4 | Activity not in `get_attractions` results | `hallucinated_activity` |

All 4 pass in isolation (`tests/unit/test_evaluator.py`), plus a combined
fixture showing `evaluate_itinerary()` reports multiple simultaneous
failures rather than short-circuiting on the first one.

## Next version (v2 / Phase 12)

Once ItineraryBuilder exists: wire `EvaluatorAgent` into the orchestrator
graph as a node after the builder runs, add the fallback-phrase allowlist,
extend hallucination checking to hotel/flight slots, and add the
`route_after_evaluation()` conditional edge (`"passed"` → persist itinerary,
`"retry"` → loop to `next_agent_for_failures()`, `"failed"` → mark trip
`TripStatus.FAILED`).

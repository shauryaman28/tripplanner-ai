# Phase 13 — Persistence: Storing Every Run

**Status: ✅ Complete**
**Done criterion:** Full pipeline produces ≥ 7 `agent_runs` rows. `GET /trips/{id}/runs` returns them ordered by `created_at` ascending. Every row has non-null `duration_ms`. A re-planned trip has additional rows. Status lifecycle correct. `GET /trips?status=completed` and `?status=failed` filter correctly. `GET /trips/{id}/timeline` returns correct ordered event log.

## What was built

```
src/ai/orchestrator/
└── orchestrator.py     ← 4 silent nodes now log: intent_parsing, persist,
                           escalate, builder_failed

src/backend/app/api/routes/
└── trips.py            ← GET /trips gains ?status= filter
                           GET /trips/{id}/timeline added

tests/unit/
└── test_phase13.py     ← 10 tests (Dev A instrumentation + Dev B endpoints)

docs/
└── phase13_build_log.md
```

## Dev A — Instrumentation audit

### Previously silent nodes (now fixed)

| Node | Why it needed logging | What it logs |
|---|---|---|
| `intent_parsing_node` | First LLM decision in every run — invisible without a row | `input={raw_input}`, `output={extracted_fields, pass_through}` |
| `persist_node` | Write decision (itinerary_id + status=completed) — the success marker | `input={total_cost, days_count}`, `output={itinerary_id, trip_status}` |
| `escalate_node` | Terminal decision on budget conflict path | `input={flight_cost, remaining_budget}`, `output={reason, options_offered, trip_status}` |
| `builder_failed_node` | Terminal decision on retry-exhausted path | `input={evaluator_retry_count, builder_error_code}`, `output={builder_error, trip_status}` |

### Already-instrumented nodes (confirmed correct)

| Node | Logged by |
|---|---|
| `flight_agent` | `FlightAgent.run()` → `log_agent_run` |
| `budget_decision` | `budget_decision_node` direct call |
| `hotel_agent` | `HotelAgent.run()` → `log_agent_run` |
| `activities_agent` | `ActivitiesAgent.run()` → `log_agent_run` |
| `itinerary_builder` | `ItineraryBuilder.run()` → `log_agent_run` |
| `evaluator` | `EvaluatorAgent.run()` → `log_agent_run` |
| `orchestrator` | `OrchestratorAgent.run()` wrapper |

### `merge_node` — intentionally not logged

`merge_node` is a pure publish step (publishes `planning_complete` SSE event) with no decision-making. Adding a log row here would add noise without diagnostic value. This is documented here rather than left unexplained.

### Minimum row count per run path

| Path | Rows |
|---|---|
| Happy path | intent_parsing + flight + budget + hotel + activities + builder + evaluator + persist + orchestrator = **9** |
| Escalate path | intent_parsing + flight + budget + escalate + orchestrator = **5** |
| Builder-failed path | intent_parsing + flight + budget + hotel + activities + builder + evaluator + builder_failed + orchestrator = **9** |
| Re-plan (1 attempt) | All happy-path rows + extra flight + budget = **11** |

All paths exceed the roadmap's "≥ 7" criterion.

## Dev B — Trip status lifecycle & endpoints

### Status lifecycle (confirmed complete)

```
POST /trips              → trips.status = "pending"
POST /trips/{id}/plan    → trips.status = "planning"  (immediately, before background task)
  persist_node           → trips.status = "completed" (on success)
  escalate_node          → trips.status = "failed"    (on budget conflict)
  builder_failed_node    → trips.status = "failed"    (on retry exhaustion)
```

The status is always written atomically in the same `commit()` as the
associated row (itinerary on success, or independently on failure paths).
`GET /trips` reads the current DB value — never stale.

### GET /trips?status= filter

Implemented with an optional `?status=` query parameter (`alias="status"` in FastAPI).
Unknown status values return an empty list — consistent with a filter that matches nothing,
avoids the need to validate the enum at the route level.

### GET /trips/{id}/timeline

Returns a merged, sorted list of `agent_run` and `itinerary_saved` events.
Each entry has:
- `event_type`: `"agent_run"` | `"itinerary_saved"`
- `label`: human-readable string with a status icon (✓/✗/…)
- `timestamp`: ISO 8601 from `created_at`
- `status`: `"completed"` | `"failed"` | `"pending"`
- `duration_ms`: for agent_run events
- `detail`: agent-specific summary dict (counts, costs, error codes)

The `_run_detail()` helper extracts a summary per agent rather than
exposing raw `output` JSON — keeps the timeline readable without a
separate frontend transform.

## Done criterion checklist

- [x] Full pipeline produces ≥ 9 `agent_runs` rows (tested with full mocked run)
- [x] `GET /trips/{id}/runs` returns rows ordered by `created_at` ascending (unchanged, confirmed)
- [x] Every row has non-null `duration_ms` (all 4 new nodes use `timed_run()`)
- [x] Re-planned trip produces additional rows (extra flight + budget rows per replan loop)
- [x] `trips.status` lifecycle: pending → planning → completed | failed (all branches covered)
- [x] `GET /trips` returns current status (reads from DB, not stale cache)
- [x] `GET /trips?status=completed` filters correctly
- [x] `GET /trips?status=failed` filters correctly
- [x] `GET /trips/{id}/timeline` returns correctly ordered event log with human-readable labels
- [x] `GET /trips/{id}/timeline` requires auth, returns 404 for wrong user
- [x] 10 unit tests pass, zero network calls
- [x] DECISIONS.md updated (entry 28)

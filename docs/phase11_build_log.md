# Phase 11 — Evaluator Agent: Self-Checking

**Status: ✅ Complete (standalone module — orchestrator wiring deferred to Phase 12, see note below)**

**Done criterion:** 4 known-bad itinerary fixtures (one per failure type) all
caught with correct failure type in `EvaluatorVerdict.failures`. Retry cap
enforced at 3. Evaluator run logged into `agent_runs`, queryable via
`GET /trips/{id}/runs`. `get_retry_chain()` reconstructs the retry timeline.

## Scope note

The roadmap describes the Evaluator as checking "the draft itinerary before
ItineraryBuilder runs" — but ItineraryBuilder itself is Phase 12, which
doesn't exist yet in this codebase. Rather than force premature integration
into `orchestrator.py` (which would mean writing throwaway/mocked builder
glue now that gets rewritten next phase), Phase 11 delivers:

- The full `EvaluatorAgent` module, fully tested against the exact JSON
  schema Phase 12's roadmap entry defines for the builder's output.
- Full `agent_runs` logging + retry-chain reconstruction (Dev B).
- Zero changes to `orchestrator.py` — there's no builder node to sit after.

Wiring `EvaluatorAgent` into the graph's conditional edges happens in
Phase 12 alongside `ItineraryBuilder`, since that's when there's an actual
node in the middle to route around.

## What was built
```
src/ai/agents/
└── evaluator.py ← EvaluatorAgent + 4 pure check_* functions
+ evaluate_itinerary() + retry routing
src/ai/utils/
└── run_logger.py ← + get_retry_chain(db, trip_id)
prompts/
└── evaluator_v1.md ← documents all 4 checks + design decision
tests/unit/
├── test_evaluator.py ← 18 tests (4 fixtures × isolated + combined
│ + retry routing + DB logging)
└── test_run_logger.py ← + 1 test for get_retry_chain
tests/integration/
└── test_phase11_integration.py ← retry-chain reconstruction against real Postgres
```

## The four checks

| Check | Pure function | Trigger condition |
|---|---|---|
| `date_out_of_range` | `check_activity_dates()` | day's `date` outside `[trip.start_date, trip.end_date]` |
| `budget_mismatch` | `check_budget_consistency()` | `total_cost` vs. `estimate_budget` total differs by >5% |
| `duplicate_activity` | `check_duplicate_activities()` | same activity name in >1 slot on the same day |
| `hallucinated_activity` | `check_hallucinated_activities()` | activity name not in `get_attractions` results |

`evaluate_itinerary()` runs all four and returns every failure found, not
just the first — lets `next_agent_for_failures()` pick the most useful
single retry target even when multiple checks fail at once.

## Why deterministic, not Claude Haiku

See `DECISIONS.md` entry 21 and `prompts/evaluator_v1.md`. Short version:
these four conditions are objectively computable (dates, arithmetic, set
membership) — same category of decision the Phase 7 router and Phase 10
`make_budget_decision()` already made deterministic, for the same reasons.

## Retry mapping & cap
```
budget_mismatch → retry flight_agent
date_out_of_range / duplicate_activity /
hallucinated_activity → retry activities_agent
retry_count >= MAX_EVALUATOR_RETRIES (3) → route_after_evaluation() = "failed"
```

## `get_retry_chain()`

Queries `agent_runs` for a trip ordered by `created_at`, and assigns each
row a per-agent-name running attempt counter. Example output for a 2-retry
flight/evaluator loop:

```python
[
  {"agent_name": "flight_agent", "attempt": 1, "status": "completed", ...},
  {"agent_name": "evaluator",    "attempt": 1, "status": "failed",    ...},
  {"agent_name": "flight_agent", "attempt": 2, "status": "completed", ...},
  {"agent_name": "evaluator",    "attempt": 2, "status": "completed", ...},
]
```

No schema migration needed — attempt numbers are derived from row order,
not stored as a column, keeping this a zero-migration phase.

## Done criterion checklist

- [x] 4 known-bad fixtures each caught with the correct `EvaluatorFailure.check` value
- [x] `evaluate_itinerary()` reports multiple simultaneous failures, not just the first
- [x] `next_agent_for_failures()` maps each failure type to the correct sub-agent, with `budget_mismatch` prioritized
- [x] `route_after_evaluation()` returns `"failed"` once `retry_count >= MAX_EVALUATOR_RETRIES` (hard cap, tested)
- [x] `EvaluatorAgent.run()` writes exactly one `agent_runs` row per call, `status` reflecting `verdict.passed`
- [x] `get_retry_chain()` reconstructs correct ordering + per-agent attempt numbers (unit-tested with mocked rows, integration-tested against real Postgres)
- [x] `prompts/evaluator_v1.md` documents all 4 failure types + the deterministic-vs-LLM decision
- [x] DECISIONS.md updated (entry 21)
- [x] Zero schema migrations, zero changes to `orchestrator.py` (scope explicitly deferred to Phase 12, documented above — not silently skipped)

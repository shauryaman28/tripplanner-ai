# Phase 10 — Budget Conflict & Re-Planning

**Status: ✅ Complete**
**Done criterion:** Scenario "5 days Goa, ₹40,000 budget, flights ₹28,000" → escalate.
Unit test: 70% budget on flights → escalate. 40% → continue. LangGraph graph shows branch.
Budget conflict SSE event arrives before `planning_complete`. `POST /trips/{id}/replan` works.

## What was built
```
src/ai/agents/
└── budget_decision.py ← BudgetDecision model + make_budget_decision() pure fn
src/ai/orchestrator/
└── orchestrator.py ← New graph structure (run_flight → budget_decision → route)
src/backend/app/
├── schemas/trip.py ← ReplanRequest schema added
└── api/routes/trips.py ← POST /trips/{id}/replan endpoint added
tests/unit/
├── test_budget_decision.py ← 10 pure-function tests (no mocks)
└── test_orchestrator.py ← Updated: fan_out replaced by new node tests
```

## Graph structure (Phase 10)
```
intent_parsing_node
↓
run_flight_node ← FlightAgent only (was part of fan_out in Phase 9)
↓
budget_decision_node ← make_budget_decision() pure fn + DB log
↓ (conditional edge)
┌─── "continue" ──────→ hotel_activities_node → merge_node → END
├─── "replan" ──────→ run_flight_node (loop, cap = 2)
└─── "escalate" ──────→ escalate_node → END
```

## Why fan_out was split

Phase 9 ran all 3 agents concurrently. Phase 10 requires checking the budget
after flights are known but before hotels are searched — hotels need
`remaining_budget` as their nightly cap. The added latency (one sequential step)
is justified by avoiding wasted hotel + activity API calls when the budget is
already blown.

## Budget thresholds

| Remaining fraction | Decision | Rationale |
|---|---|---|
| ≥ 50% | continue | Comfortable |
| 35–49% | replan | Borderline — try cheaper flights |
| < 35% | escalate | Unviable — less than ₹14k on a ₹40k budget |

Verification:
- ₹40k budget, ₹28k flights → 30% remaining → **escalate** ✓
- ₹40k budget, ₹16k flights → 60% remaining → **continue** ✓

## Replan loop

On "replan", `budget_decision_node` increments `replan_attempts` and routes
back to `run_flight_node`. The node uses `replan_flight_budget()` to lower
the budget cap:
- Attempt 1 → 65% of original budget (find connecting flights)
- Attempt 2 → 55% of original budget (last resort)
- Attempt ≥ 2 → always escalate

## SSE event sequences

Happy path:
`planning_started → flight_agent:completed → hotel_agent:completed → activities_agent:completed → planning_complete`

Escalate path:
`planning_started → flight_agent:completed → budget_conflict → planning_failed`

## New endpoint

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/trips/{id}/replan` | `{"choice": "cheaper_flights"\|"reduce_days"\|"increase_budget"}` | 200 `replanning_started` |

## Done criterion checklist

- [x] `make_budget_decision(flights_at_70_pct, 40000)` → `decision="escalate"` (pure fn test)
- [x] `make_budget_decision(flights_at_40_pct, 40000)` → `decision="continue"` (pure fn test)
- [x] `budget_decision_node` increments `replan_attempts` on "replan"
- [x] Replan cap: `replan_attempts >= 2` → always escalate
- [x] `budget_conflict` SSE event published before `planning_failed`
- [x] Hotel + activities not called on escalate path (verified in test)
- [x] `budget_decision` dict persisted in `agent_runs.output` for the orchestrator row
- [x] `POST /trips/{id}/replan` re-runs Orchestrator with adjusted params
- [x] LangGraph graph visualisable (separate escalate/replan/continue branches)
- [x] 10 budget_decision unit tests pass (pure function, zero mocks)
- [x] 9 orchestrator tests pass
- [x] DECISIONS.md updated (entries 17–20)

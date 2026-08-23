# Phase 12 — Itinerary Builder: Claude Haiku + Structured Synthesis

**Status: ✅ Complete**

**Done criterion:** Full run on "Plan a 7-day trip to Goa in December for 2 people, budget ₹50,000" → complete `Itinerary` saved in DB with all days populated, morning/afternoon/evening slots assigned, hotel assigned per night, flight cost included, total_cost within budget, zero placeholder strings. Prompt on v3+. Itinerary readable via `GET /trips/{id}/itinerary`.

## What was built

```
src/ai/
├── builder/
│   ├── __init__.py          ← exports ItineraryBuilder, ItineraryDraft, etc.
│   └── builder.py           ← ItineraryBuilder (Claude Haiku) + data-scope & budget-math validation
├── orchestrator/
│   └── orchestrator.py      ← full build → evaluate → retry/persist/fail graph wiring
└── utils/
    └── embeddings.py        ← generate_embeddings stub (lands Phase 14)

prompts/
├── itinerary_builder_v1.md  ← baseline synthesis prompt
├── itinerary_builder_v2.md  ← empty-data fallback rule
└── itinerary_builder_v3.md  ← explicit "compute total_cost last" instruction

tests/
├── unit/
│   ├── test_itinerary_builder.py    ← 6 tests (schema validation, scope violation, budget math, error handling)
│   └── test_orchestrator.py         ← updated with build/evaluate/retry/persist node & routing tests
└── integration/
    └── test_phase12_integration.py  ← build → evaluate → atomic persist against real Postgres
```

## Graph structure (Phase 12 complete)

```
intent_parsing_node
      ↓
run_flight_node
      ↓
budget_decision_node
      ↓ (conditional: route_after_budget_decision)
┌─── "continue" ──────→ hotel_activities_node
├─── "replan"   ──────→ run_flight_node (loop, cap = 2)
└─── "escalate" ──────→ escalate_node → END

hotel_activities_node
      ↓
build_itinerary_node
      ↓
evaluate_node
      ↓ (conditional: route_after_evaluator)
┌─── "passed" ─────────→ persist_node → merge_node → END
├─── "retry"  ─────────→ retry_dispatch_node → build_itinerary_node (loop, cap = 3)
└─── "failed" ─────────→ builder_failed_node → END
```

## Done criterion checklist

- [x] `ItineraryBuilder` produces valid `ItineraryDraft` matching day-by-day schema
- [x] Data-scope validator catches hallucinated activities and hotels before evaluator
- [x] Budget-math validator ensures day costs + flights equal total_cost within ₹500
- [x] Prompt iterated through v1, v2, v3 with documented failure modes and test results
- [x] `EvaluatorAgent` (Phase 11) wired into orchestrator graph
- [x] Retry loop dispatches to implicated sub-agent and re-runs builder up to 3 times
- [x] `persist_node` atomically writes `Itinerary` and updates `Trip.status = COMPLETED`
- [x] `generate_embeddings` stub triggered on persist
- [x] Integration tests confirm atomic rollback on persist failure
- [x] DECISIONS.md updated (entries 24–27)
- [x] README.md updated

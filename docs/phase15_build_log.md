# Phase 15 Build Log — Multi-Turn Refinement

**Date:** 2026-07-25  
**Status:** ✅ Complete — 175/175 tests passing

---

## Goal

Enable users to send follow-up messages after an initial plan ("make it cheaper", "switch to a nicer hotel") and receive a revised itinerary without re-running the full planning pipeline from scratch.

---

## Files Changed / Created

| File | Type | Change |
|---|---|---|
| `migrations/versions/002_add_turn_to_agent_runs.py` | NEW | Alembic migration: adds `turn` column (default=1) + `ix_agent_runs_turn` index |
| `src/backend/app/models/agent_run.py` | MODIFY | Added `turn: int = Field(default=1)` |
| `src/backend/app/schemas/agent_run.py` | MODIFY | Added `turn: int = 1` to `AgentRunRead` |
| `src/ai/utils/conversation.py` | MODIFY | `append_history()` gains `turn=1` param; new `get_current_turn()` helper |
| `src/ai/utils/run_logger.py` | MODIFY | `log_agent_run()` gains `turn=1` param; `get_retry_chain()` exposes `turn` |
| `src/ai/agents/refinement_classifier.py` | NEW | `RefinementClassifier` — 5 action types, Gemini Flash at temperature=0, full_replan fallback |
| `src/ai/agents/flight_agent.py` | MODIFY | `run()` gains `turn=1` param |
| `src/ai/agents/hotel_agent.py` | MODIFY | `run()` gains `turn=1` param |
| `src/ai/agents/activities_agent.py` | MODIFY | `run()` gains `turn=1` param |
| `src/ai/agents/evaluator.py` | MODIFY | `run()` gains `turn=1` param |
| `src/ai/builder/builder.py` | MODIFY | `run()` gains `turn=1` param |
| `src/ai/orchestrator/orchestrator.py` | MODIFY | `turn` in `OrchestratorState`; all nodes propagate it; new `refine()` method |
| `src/backend/app/api/routes/trips.py` | MODIFY | `GET /runs?turn=N` filter; new `POST /trips/{id}/refine` endpoint |
| `prompts/refinement_classifier_v1.md` | NEW | Classification types, hard rules, hard-case rationale |
| `tests/unit/test_phase15_refinement.py` | NEW | 16 unit tests — zero network calls |

---

## Architecture

### Turn propagation chain

```
POST /trips/{id}/refine
  └─ get_current_turn() → next_turn = current + 1
  └─ classify_refinement() → RefinementType
  └─ append_history(..., turn=next_turn)
  └─ OrchestratorAgent.refine(turn=next_turn)
       └─ [targeted agent].run(..., turn=next_turn)
       └─ ItineraryBuilder.run(..., turn=next_turn)
       └─ EvaluatorAgent.run(..., turn=next_turn)
       └─ persist_node → log_agent_run(..., turn=next_turn)
```

Every `agent_runs` row written during the refinement pass carries the same `turn` value, enabling `GET /trips/{id}/runs?turn=2` to isolate second-pass rows.

### RefinementClassifier

Five action types:

| Type | Re-runs | Carries forward |
|---|---|---|
| `full_replan` | All 3 agents | Nothing |
| `targeted_flights` | FlightAgent | Hotels + activities |
| `targeted_hotel` | HotelAgent | Flights + activities |
| `targeted_activities` | ActivitiesAgent | Flights + hotels |
| `add_day` | All 3 agents (new dates) | Nothing |

Hard rules embedded in prompt:
1. "Make it cheaper" → `targeted_flights`
2. Any duration extension → `add_day`
3. Any destination change → `full_replan`

On LLM failure → falls back to `full_replan` (safe default).

### refine() bypass

`OrchestratorAgent.refine()` does NOT run the LangGraph. It:
1. Carries forward unaffected agent results from `prior_state`
2. Calls only the targeted sub-agent(s)
3. Runs the shared tail: `build_itinerary_node → evaluate_node → [retry loop] → persist_node`

This avoids re-running `budget_decision` and `intent_parsing` for targeted changes where the trip metadata is already known.

### Itinerary versioning

`persist_node` always INSERTs a new row (never UPDATEs). Turn 1 and turn 2 itineraries both exist in the DB. `GET /trips/{id}/itinerary` returns the latest by `created_at`; all versions are accessible via `GET /trips/{id}/itineraries`.

---

## Backward Compatibility

All `turn` parameters default to `1`. All existing callers (Phases 6–14) require zero changes. Pre-Phase-15 `agent_runs` rows get `turn=1` via the migration's `server_default="1"`.

---

## Test Coverage

| Group | Tests | Notes |
|---|---|---|
| RefinementClassifier | 6 | All 5 types + fallback; mocked LLM |
| Conversation history | 4 | turn field, get_current_turn, backward compat |
| run_logger | 2 | turn writes correctly, chain includes turn |
| API endpoints | 4 | turn filter on /runs, /refine 409 + 200 |
| **Total Phase 15** | **16** | **16/16 passing** |
| **Total suite** | **175** | **175/175 passing** |

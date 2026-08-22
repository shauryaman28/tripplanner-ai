# Phase 7 — Conditional Edges: Ask Instead of Assume

**Status: ✅ Complete**
**Done criterion:** Ambiguous query → clarifying question → user answers → correct tool call. Router is deterministic. 8 router unit tests pass. API correctly distinguishes `planning_started` vs `clarification_needed`. Prompt on v3.

## What was built

### Phase 7A — Intent Parser & Router (Dev A)

```
src/ai/agents/flight_agent.py    ← Updated: 3-node graph (was single-node)
  - intent_parsing_node          ← LLM extracts fields from free text
  - router                       ← Conditional edge (pure Python, no LLM)
  - search_flights_node          ← Unchanged from Phase 6
  - clarify_node                 ← Returns deterministic clarifying question

prompts/
├── flight_agent_v2.md           ← Intent parsing prompt documented
└── flight_agent_v3.md           ← Multi-turn clarification documented
```

### Phase 7B — Clarification State & API (Dev B)

```
src/ai/utils/conversation.py     ← NEW: Redis-backed history + state storage
src/backend/app/schemas/trip.py  ← Updated: PlanRequest, ClarifyRequest
src/backend/app/api/routes/trips.py ← Updated: plan_trip rewired, clarify_trip added

tests/unit/
├── test_flight_agent_router.py  ← 8 tests (router, clarify_node, intent parser)
└── test_phase7b_clarification.py ← 5 tests (plan + clarify API flow)
```

## Three-node graph

```
                    ┌─── "search" ──→ search_flights_node ──→ END
intent_parsing ──→ router
                    └─── "clarify" ──→ clarify_node ──→ END
```

- **`intent_parsing_node`:** Gemini Flash (`temperature=0`) extracts structured fields from `raw_input`. If no `raw_input`, passes state through unchanged (backward-compatible with Phase 6 structured callers).
- **`router`:** Pure Python function — checks `state.get(field)` for `destination`, `date`, `budget`. Returns `"search"` or `"clarify"`. **No LLM in routing** — this is explicitly a conditional edge.
- **`clarify_node`:** Deterministic question based on first missing field. No LLM, no tool call.

## API contract

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/trips/{id}/plan` | `{"raw_input": "..."}` (optional) | `{"status": "planning_started"}` or `{"status": "clarification_needed", "question": "..."}` |
| POST | `/trips/{id}/clarify` | `{"answer": "..."}` | Same as above |

### Clarification flow

1. `POST /plan` with ambiguous `raw_input` → intent parser extracts partial fields → router returns `"clarify"` → response: `clarification_needed`
2. Partial state saved to Redis (`trip:{id}:planning_state`)
3. `POST /clarify` with user's answer → state restored, answer merged as `raw_input` → intent parser fills in missing fields → router decides again
4. If all fields present → `"search"` → flights returned → `planning_started`
5. If still missing → another `clarification_needed` (supports multi-round)

## The `not state.get(field)` fix

**Before (broken):** `if value is not None and field not in state`
**After (fixed):** `if value is not None and not state.get(field)`

Without this fix, a state like `{"date": None}` would never get `date` filled on retry because `"date" in state` is `True` even when the value is `None`. See `prompts/flight_agent_v3.md` for full explanation.

## Redis state management

| Key pattern | Content | TTL |
|---|---|---|
| `trip:{id}:conv_history` | `[{"role": "user", "content": "..."}, ...]` | 24h |
| `trip:{id}:planning_state` | Serialized `TripState` dict | 24h |

Both use 24h TTL matching JWT expiry — ephemeral session data, not durable storage.

## Key decisions (see DECISIONS.md)

6. Router is deterministic — conditional edge, not LLM node
7. `clarify_node` is deterministic — hardcoded questions, no LLM
8. Intent parsing uses Gemini Flash at `temperature=0`
9. `not state.get(field)` over `field not in state` for retry correctness
10. Conversation state in Redis, not Postgres

## Done criterion checklist

- [x] `router()` with `destination=None` → returns `"clarify"` (5 router tests)
- [x] `router()` with all fields → returns `"search"`
- [x] LLM mocked in all router tests — router is pure Python
- [x] 8 router unit tests pass
- [x] Ambiguous query "somewhere warm in December" → router returns `"clarify"`, no tool call
- [x] `conversation_history: list[dict]` added to `TripState`
- [x] `POST /trips/{id}/plan` → `clarification_needed` when graph returns clarification
- [x] `POST /trips/{id}/clarify` → re-runs graph with updated state
- [x] Conversation history preserved in Redis across both calls
- [x] API correctly distinguishes `planning_started` vs `clarification_needed`
- [x] Prompt on v3 (`prompts/flight_agent_v3.md`)
- [x] DECISIONS.md updated with Phase 7 decisions

# Phase 9 — Orchestrator: Decomposition & Fan-Out

**Status: ✅ Complete**
**Done criterion:** Full run on "Plan a 7-day trip to Goa in December for 2 people, budget ₹50,000" → all 3 sub-agents called concurrently (overlapping `created_at` in `agent_runs`). Results merged in OrchestratorState. Prompt on v3+. 5 unit tests pass.

## What was built

```
src/ai/orchestrator/
└── orchestrator.py          ← OrchestratorAgent: 3-node LangGraph graph

prompts/
├── orchestrator_v1.md       ← Baseline extraction
├── orchestrator_v2.md       ← Budget normalisation + date resolution
└── orchestrator_v3.md       ← Mixed-language interests + fan-out rationale

tests/unit/
└── test_orchestrator.py     ← 5 tests (all sub-agents mocked)

src/backend/app/api/routes/trips.py  ← Updated: POST /plan now uses OrchestratorAgent
```

## OrchestratorAgent graph

```
intent_parsing_node ──→ fan_out_node ──→ merge_node ──→ END
```

| Node | LLM | Side effects |
|---|---|---|
| `intent_parsing_node` | Gemini Flash | None (pure extraction) |
| `fan_out_node` | None (delegates to sub-agents) | asyncio.gather concurrent execution; SSE events published |
| `merge_node` | None | SSE planning_complete event published |

## Concurrency implementation

`fan_out_node` uses `asyncio.gather(return_exceptions=True)`:
- All three sub-agents start in the same event loop tick → overlapping wall-clock time → confirmed via `agent_runs.created_at` timestamps
- `return_exceptions=True` means one agent raising an exception never cancels the other two
- Result processing is explicit: Exception → `status="failed"`, dict with error → `status="failed"`, dict with data → `status="completed"`

**Why not LangGraph `Send` API:** `Send` is for dynamic fan-out to the same node. Three different agent types with different state shapes → `asyncio.gather` is simpler, equally concurrent, and easier to unit-test. Decision documented in `DECISIONS.md` entry 15 and `prompts/orchestrator_v3.md`.

## Partial failure behaviour

| Scenario | Outcome |
|---|---|
| All 3 succeed | `flight_status=hotel_status=activities_status="completed"` |
| One fails | Other two results preserved; failed agent has `status="failed"` + error |
| All fail | All statuses="failed"; no exception raised; route gets empty lists |

## SSE event sequence

```
planning_started        (published immediately by route, before Orchestrator runs)
  → agent: flight_agent, status: completed/failed    (from fan_out_node)
  → agent: hotel_agent, status: completed/failed
  → agent: activities_agent, status: completed/failed
  → event: planning_complete                          (from merge_node)
```

## Route changes (trips.py)

- `POST /trips/{id}/plan` now runs `OrchestratorAgent` instead of `FlightAgent` directly
- Orchestrator runs in `asyncio.create_task` so HTTP response is 202 immediately
- `planning_started` SSE event published before the background task starts
- `_run_orchestrator` background helper catches all exceptions and publishes `planning_failed` via SSE so clients don't hang

## Key decisions (DECISIONS.md entries 15–16)

15. **`asyncio.gather` over LangGraph `Send` for fan-out** — identical concurrency, simpler error handling, easier unit testing. Documented in `orchestrator_v3.md`.
16. **OrchestratorAgent injects `publish_fn` as state** — keeps the agent layer infrastructure-agnostic. The route creates the closure over the Redis client; the agent just calls `await publish_fn(event)`. This means unit tests can pass a plain `AsyncMock` without any Redis setup.

## Done criterion checklist

- [x] `OrchestratorAgent.run({"destination": "Goa", "start_date": "2026-12-10", "end_date": "2026-12-17", "budget": 50000, "group_size": 2, "interests": ["beach", "food"]})` → all 3 sub-agents called, results in OrchestratorState
- [x] Sub-agents run concurrently (verified via `asyncio.gather`; `agent_runs.created_at` overlaps in integration)
- [x] Results merged into `OrchestratorState`
- [x] Orchestrator prompt on v3+
- [x] 5 unit tests pass (intent_parsing passthrough, full happy path, partial failure, all-fail no-exception, SSE events)
- [x] SSE events published per-agent + planning_complete
- [x] `planning_started` SSE event published immediately at route level
- [x] `planning_failed` SSE event on unhandled exception (no silent hang)
- [x] Route returns 202 before Orchestrator finishes (background task)
- [x] DECISIONS.md updated (entries 15–16)

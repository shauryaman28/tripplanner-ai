# DECISIONS.md

> One-line-per-decision log. Updated after each phase.
> A decision is not "we used X" — it is "we chose X **over Y because Z**."

---

## Phase 6 — FlightAgent: One Agent, One Tool

1. **LangGraph node contract:** every node receives a `TripState` (TypedDict) and returns a `TripState`. Partial updates merge via `{**state, **updates}` — nodes only need to return the fields they change. This is LangGraph's native state-merge behavior, not a custom pattern.

2. **No LLM in Phase 6 search node:** `search_flights_node` is a pure function — structured input → MCP call → structured output. We deliberately avoided wrapping this in an LLM call because structured inputs don't need interpretation, and adding an LLM here would introduce latency and hallucination risk for zero benefit.

3. **MCP client as singleton subprocess:** the MCP server runs as a child process (stdio transport) with one persistent `ClientSession`. The session is lazily initialized on first `call_tool()` and reused for the process lifetime. This avoids subprocess spawn overhead on every tool call. Cleanup is wired into FastAPI's lifespan shutdown.

4. **`ToolError` as Pydantic model, never raw exceptions:** `call_tool()` catches all exceptions and returns a `ToolError` model. Agent nodes check `hasattr(result, "code")` to detect errors. This means agents never crash from MCP failures — they populate `state["error"]` instead.

5. **`agent_runs` as the debugging table:** every agent execution writes one row with `input`, `output`, `duration_ms`, and `status` as JSONB. This is intentionally over-logging — when the re-planning logic breaks in Phase 10+, we query `agent_runs` for the trip and read every decision without writing additional logging code.

---

## Phase 7 — Conditional Edges: Ask Instead of Assume

6. **Router is deterministic — no LLM in the routing decision.** The `router()` function is a plain Python function that checks `state.get(field)` for required fields and returns `"search"` or `"clarify"`. This is explicitly implemented as a LangGraph conditional edge, not a node with an LLM call. Reason: routing logic is simple boolean checks. An LLM here would add latency, cost, and non-determinism for a decision that is fully specified by data presence.

7. **`clarify_node` is also deterministic — no LLM.** It returns a hardcoded question based on which field is missing first. We chose this over an LLM-generated question because: (a) the question set is small and known, (b) deterministic questions are unit-testable without mocking, (c) response time is instant.

8. **Intent parsing uses Gemini Flash at temperature=0.** For extracting structured fields from free text, we use `gemini-1.5-flash` with `temperature=0` to maximize determinism. The prompt demands JSON-only output with null for missing fields. Markdown fence stripping handles the model's tendency to wrap JSON in ` ```json ``` ` blocks.

9. **`not state.get(field)` over `field not in state` for retry correctness.** After the first `/plan` returns a clarification, saved state has keys with `None` values (e.g., `{"date": None}`). The original `field not in state` check treats this as "field exists" and skips it on retry — causing an infinite clarification loop. Changed to `not state.get(field)` which treats `None` as "fill this in."

10. **Conversation state in Redis, not Postgres.** Planning state and conversation history are stored in Redis with 24h TTL (matching JWT expiry). Rationale: this is ephemeral session data that expires when the auth token does. Postgres is for durable data (trips, itineraries, agent_runs). Redis gives sub-millisecond reads during the multi-turn clarification loop without polluting the relational schema.

---

## Phase 8 — HotelAgent & ActivitiesAgent

11. **HotelAgent uses a fresh prompt — not copied from FlightAgent.** Hotel search requires fundamentally different field semantics: `check_in`/`check_out` vs. a single `date`; `budget_per_night` (per room per night) vs. total `budget`; `guests` vs. `passengers`. Copying the flight prompt and renaming fields produces subtle extraction bugs that are hard to catch in mocked unit tests but visible in production (e.g. LLM interprets "departure date" as check-out, or divides budget incorrectly). Starting fresh eliminated 2 of 5 test failures that appeared when a shared prompt was tried first.

12. **ActivitiesAgent router requires BOTH `destination` AND non-empty `interests`.** Unlike FlightAgent (destination, date, budget) and HotelAgent (destination, check_in, check_out, budget_per_night), the activities router treats an empty `interests` list identically to `None`. Rationale: calling `get_attractions` with an empty interests list produces a near-useless query ("top  attractions in Goa") that returns generic sightseeing results regardless of what the user actually wants. Forcing the clarification produces a better result than silently falling back to generic.

13. **Non-English interests pass through to the MCP tool unchanged at the agent layer.** The `intent_parsing_node` instructs Gemini Flash to translate non-English interests (e.g. Hindi "खाना" → "food") when processing `raw_input`. However, `get_attractions_node` does NOT validate or filter interests — it passes whatever is in state directly to the MCP tool, which embeds them verbatim in the Google Maps query string. This is intentional: the agent layer should not own language concerns; the MCP tool is the correct boundary. The documented failure mode is "fewer or less relevant results" rather than a crash, and the test `test_non_english_interest_passes_through_without_crash` verifies this contract.

14. **Each agent owns its own independent TypedDict state — no shared monolith.** `HotelState` and `ActivitiesState` are separate TypedDicts rather than one merged `TripState` covering all agents. This keeps each agent self-contained and unit-testable without needing to mock fields irrelevant to it. The OrchestratorAgent (Phase 9) will extract and pass only the relevant state slice to each sub-agent, acting as the state coordinator. Merging all fields into one TypedDict now would make every agent implicitly dependent on every other agent's field set.

---

## Phase 9 — Orchestrator: Decomposition & Fan-Out

15. **`asyncio.gather` over LangGraph `Send` API for concurrent fan-out.** LangGraph's `Send` API is designed for dynamic fan-out to the same node type with different inputs. For three different agent types (FlightAgent, HotelAgent, ActivitiesAgent) with different state shapes, `Send` requires compiled subgraphs and makes merge logic significantly more complex. `asyncio.gather(return_exceptions=True)` gives identical wall-clock concurrency (all three sub-agents start in the same event loop iteration), simpler error handling per-result, and unit tests that need no LangGraph infrastructure. Concurrency is verified by overlapping `agent_runs.created_at` timestamps in integration tests.

16. **`publish_fn` injected into `OrchestratorState` rather than importing Redis directly in the agent.** The Orchestrator publishes SSE progress events via an async callable passed in by the route. This keeps the agent layer infrastructure-agnostic — unit tests pass a plain `AsyncMock` with no Redis setup. The route creates the closure over the Redis client before calling `agent.run()`. The pattern is the same as the `db` and `trip_id` injection used by sub-agents since Phase 6, keeping all agents consistent.

---

## Phase 10 — Budget Conflict & Re-Planning

17. **Sequential flight → budget check → hotel+activities over concurrent fan-out.** Phase 9's `fan_out_node` ran all 3 agents concurrently. Phase 10 splits this into `run_flight_node → budget_decision_node → hotel_activities_node`. The extra sequential step is justified: (a) hotels need `remaining_budget` as their nightly cap, not the full trip budget; (b) if the budget check fails we skip hotel + activities API calls entirely; (c) flights are typically the costliest and most variable line item.

18. **`make_budget_decision()` as a pure function.** All threshold logic, cap enforcement, and percentage calculations live in a zero-I/O function. The LangGraph node wraps it with DB logging. This means 10 budget-decision tests run with zero mocks — the fastest and most trustworthy kind.

19. **Thresholds: 35% / 50% remaining.** `< 35%` remaining → escalate; `35–49%` → replan; `≥ 50%` → continue. These satisfy both acceptance criteria: ₹40k budget / ₹28k flights (30% remaining) → escalate; ₹40k / ₹16k (60% remaining) → continue. The 35% floor ensures at least ₹14,000 remains on a ₹40k budget — enough for a 5-day trip at ₹1,000/night hotels + ₹500/day activities.

20. **Replan budget reduction: 65% → 55% of original.** On attempt 1 the flight budget cap drops to 65% (finds connecting/budget-carrier options); on attempt 2 to 55% (last resort). `replan_attempts` is incremented in `budget_decision_node` before routing so `run_flight_node` sees the correct attempt number and applies the right cap via `replan_flight_budget()`.

---

## Phase 11 — Evaluator Agent: Self-Checking

21. **EvaluatorAgent's four core checks are pure functions, not Claude Haiku calls.** The roadmap frames the Evaluator as an LLM-based independent check. We deliberately implemented `date_out_of_range`, `budget_mismatch`, `duplicate_activity`, and `hallucinated_activity` as deterministic Python functions instead. These are objectively verifiable conditions — date comparison, arithmetic within a tolerance, set membership, duplicate detection — and don't benefit from LLM subjectivity. An LLM call here would add cost, latency, and non-determinism for zero benefit, and would make the "4 known-bad fixtures caught with correct failure type" acceptance criterion dependent on mocked LLM output rather than genuinely-tested logic. This mirrors the Phase 7 router (#6) and Phase 10 `make_budget_decision()` (#18) precedent already established in this codebase. Claude Haiku 4.5 remains the right tool for genuinely subjective quality judgments — the roadmap's own Phase 33 eval-suite grader is that home.

22. **Evaluator is not wired into `orchestrator.py` in Phase 11.** The roadmap places the Evaluator between ItineraryBuilder's draft output and final persistence — but ItineraryBuilder is Phase 12 and doesn't exist yet. Rather than build throwaway integration glue now (mocking a builder that doesn't exist) and rewrite it next phase, Phase 11 ships `EvaluatorAgent` as a fully standalone, fully-tested module validated against the exact draft-itinerary JSON schema Phase 12's roadmap entry defines. Wiring into the graph's conditional edges happens in Phase 12 alongside the builder node it needs to sit after.

23. **`get_retry_chain()` derives attempt numbers from `created_at` ordering — no new DB column.** Rather than add a `retry_count` column to `agent_runs` (which would need an Alembic migration and would duplicate information already recoverable from row order), `get_retry_chain()` reconstructs per-agent attempt numbers by counting occurrences of each `agent_name` in chronological order. This keeps Phase 11 a zero-migration phase while still satisfying "retry is visible" in `GET /trips/{id}/runs`-style queries.

---

## Phase 12 — Itinerary Builder: Claude Haiku + Structured Synthesis

24. **ItineraryBuilder validates data scope and budget math deterministically in Python, not via LLM self-report.** Mirrors the #21 evaluator precedent, but here the checks run *inside* the builder before the draft ever reaches EvaluatorAgent — cheaper to fail fast on a malformed draft than to round-trip it through a second LLM call first.

25. **Builder failures with no draft share the Evaluator's retry cap.** Rather than a separate half-built failure lane, `route_after_evaluator` treats "no draft produced" as an automatic retry (bounded by `MAX_EVALUATOR_RETRIES`), keeping one retry cap for the whole build+evaluate loop instead of two independent caps that could double the effective retry budget.

26. **Evaluator retry dispatch reruns only the implicated sub-agent** (`flight_agent` or `activities_agent`, via `next_agent_for_failures()`) and loops directly back to `build_itinerary_node`, skipping `budget_decision`/`hotel_activities` entirely. Cheaper than re-deriving unrelated data, and matches the roadmap's "loop back to the relevant agent" wording literally.

27. **Itinerary persistence is one non-branching node (`persist_node`) using a single `AsyncSession` and a single `commit()`.** The itinerary row and the trip-status update are queued on the same session and committed together — a failure before commit leaves neither write applied (verified in `test_phase12_integration.py`). `generate_embeddings()` runs as a same-node call rather than a FastAPI `BackgroundTask` because there is no request context inside the orchestrator; Phase 14 revisits this boundary when it implements real embedding generation.

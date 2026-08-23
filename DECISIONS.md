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

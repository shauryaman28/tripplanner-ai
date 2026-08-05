# AI Trip Planner — 50-Phase Build Plan (Python / FastAPI + LangGraph, 2 Developers)

Every phase below has concrete deliverables and acceptance criteria — not vague tasks — so you can tell if you're on track. Phases 1–5 are already done (see the repo zip); they're included here for completeness and so the log of decisions stays in one place.

**Team split:**
- **Dev A — Agent Core & Platform:** FlightAgent, HotelAgent, ActivitiesAgent, OrchestratorAgent, ItineraryBuilder, Evaluator, re-planning logic, FastAPI gateway, JWT auth, MCP server wiring
- **Dev B — Data, Storage & Frontend:** Ledger/persistence layer, embeddings, SSE pipeline, Redis pub/sub, Next.js frontend, map view, PDF export, pgvector similarity, observability

**Phase ritual:** After each phase, update `DECISIONS.md` with one line per key decision and why. A phase is not done when it runs — it is done when you can break it on purpose and explain why.

**Stack reference:**
FastAPI · SQLModel + SQLAlchemy 2.0 · Alembic · asyncpg · pgvector · Redis (async) · LangGraph · MCP Python SDK · Gemini Flash (orchestration) · Claude Haiku 4.5 (itinerary + evaluator) · OpenAI text-embedding-3-small · Next.js 14 + Tailwind + shadcn/ui · Leaflet · Playwright · pytest / pytest-mock · httpx · sse-starlette · Prometheus + Grafana · OpenTelemetry

---

## Chapter 1 — Foundation (Phases 1–5) ✅ Complete

### Phase 1 — Repo & Local Infrastructure ✅ Done
- FastAPI app with `/ping` health check endpoint
- Async SQLAlchemy + asyncpg for Postgres; Async Redis client (singleton via lifespan)
- `docker-compose.yml`: Postgres (pgvector/pgvector:pg16), Redis, optionally the backend; `docker/init.sql` enabling the `vector` extension on first Postgres init
- 4 unit tests + 1 integration test for `/ping`; GitHub Actions CI: lint + unit tests on every push
- **Acceptance:** `docker compose up` → `curl /ping` → `{ "postgres": "ok", "redis": "ok" }`. Status always 200 — errors in body, never HTTP status.

### Phase 2 — MCP Server: Protocol First, Tools Second ✅ Done
- Mocked MCP server; 5 tools registered: `search_flights`, `search_hotels`, `get_attractions`, `get_weather`, `estimate_budget`
- All tools return `ToolError` on failure — no Python exceptions that crash the server; `prompts/` folder with versioning structure
- **Acceptance:** MCP Inspector shows all 5 tools, all callable, 2+ error responses verified, all 13 pytest pass

### Phase 3 — MCP Server: Real APIs ✅ Done
- All 5 tools wired to real external APIs; Redis caching with differentiated TTLs (flights 5 min, hotels 15 min, attractions 6 hr, weather 1 hr)
- `[CACHE HIT]` / `[CACHE MISS]` logging; missing key → `ToolError(code="API_NOT_CONFIGURED")`, server never crashes
- Contract tests (shape only), integration tests gated behind `RUN_INTEGRATION=1`
- **Acceptance:** All 5 tools return real data via MCP Inspector, caching verified via logs, at least one real API error handled

### Phase 4 — Database: Schema, Models, Migrations ✅ Done
- Tables: `users`, `trips`, `itineraries`, `agent_runs`, `embeddings` (pgvector `vector(1536)` + HNSW index)
- SQLModel + Alembic; `agent_runs` is the debugging table — every agent decision logged here
- **Acceptance:** `alembic upgrade head` runs clean, all 5 tables exist, all models importable, vector column is `USER-DEFINED` in psql

### Phase 5 — FastAPI Gateway: Auth, Routes, SSE ✅ Done
- Routes: `POST /auth/register`, `POST /auth/login`, `GET /trips`, `POST /trips`, `POST /trips/{id}/plan`, `GET /trips/{id}/stream` (SSE), `GET /trips/{id}/itinerary`, `GET /trips/{id}/similar` (501 until Phase 23), `GET /trips/{id}/runs`
- JWT middleware (HS256, 24h expiry); SSE skeleton wired to Redis pub/sub; SSE also accepts `?token=` for browser `EventSource`
- 40 tests total (unit + contract), all passing, zero network calls in CI
- **Acceptance:** All routes return correct status codes, JWT rejects invalid tokens, SSE delivers a manually-published Redis event to a browser tab within milliseconds

---

## Chapter 2 — The Agent Core (Phases 6–17)

### Phase 6 — FlightAgent: One Agent, One Tool

**What this phase delivers:** The first real LangGraph agent — FlightAgent — reliably calls `search_flights` via MCP with correct parameters on clear input. Prompt versioning discipline established for the whole project.

---

**Dev A — Agent Implementation:**
- Implement `FlightAgent` as a single-node LangGraph graph in `src/ai/agents/flight_agent.py`
- State definition: `TripState(TypedDict)` with fields `destination`, `origin`, `date`, `return_date`, `budget`, `passengers`, `flights` (result), `error`
- Node function `search_flights_node(state: TripState) -> TripState`: calls `search_flights` via MCP client, populates `state["flights"]` on success or `state["error"]` on `ToolError`
- Use `langchain_google_genai.ChatGoogleGenerativeAI` (Gemini Flash) as the LLM
- Create `prompts/flight_agent_v1.md` — document what the prompt says, what it does well, where it fails. Run on 5 different clear-input test cases, observe failures, fix one thing, save as `prompts/flight_agent_v2.md`. This versioning discipline applies to every LLM in the system
- Document in `DECISIONS.md`: what does a LangGraph node receive, what must it return, how does a partial state update propagate
- **Acceptance:** `FlightAgent.run({"destination": "Goa", "origin": "DEL", "date": "2025-12-10", "budget": 20000, "passengers": 1})` returns state with a non-empty `flights` list. 6 unit tests pass (4 happy paths, 2 error cases where MCP returns `ToolError`). Prompt is on at least v2.

**Dev B — MCP Client Wrapper & Agent Run Logging:**
- Build `src/ai/mcp_client/client.py` — async wrapper around the MCP Python SDK used by all agents: `async def call_tool(tool_name: str, params: dict) -> dict | ToolError`. `ToolError` responses converted to Pydantic model — agents never see raw MCP protocol errors
- Wire `agent_runs` table writes into `src/ai/utils/run_logger.py`: `log_agent_run(trip_id, agent_name, input, output, duration_ms, status)` — called by every agent node on entry and exit
- Integration test: call `FlightAgent.run(...)`, query `agent_runs`, confirm one row with `agent_name = "flight_agent"`, `status = "completed"`, non-null `duration_ms`
- **Acceptance:** `log_agent_run` writes a correctly-shaped row. MCP client correctly converts `ToolError` responses. Integration test passes (gated behind `RUN_INTEGRATION=1`).

---

### Phase 7 — Conditional Edges: Ask Instead of Assume

**What this phase delivers:** Ambiguous query → clarifying question → user answers → correct tool call. The router is deterministic even when the LLM is not.

---

**Dev A — Intent Parser & Router:**
- Add `IntentParsingNode` before `search_flights_node` in FlightAgent's graph: LLM receives the free-form user message and extracts structured fields into `TripState`
- Add `RouterNode` (a conditional edge function — not an LLM call) that reads populated state and returns `"clarify"` or `"search"`:
  - Required fields for flight search: `destination`, `date`, `budget` — if any missing or ambiguous → `"clarify"`
  - All present and unambiguous → `"search"`
- `ClarifyNode`: returns a plain-English clarifying question in `state["clarification_question"]`, does NOT call any tool
- Document in `DECISIONS.md`: the router is deterministic — no LLM in the routing decision itself. This is explicitly why it is a conditional edge, not another LLM prompt
- Update `prompts/flight_agent_v2.md` → `v3.md` to reflect the intent-parsing prompt
- **Acceptance:** Router unit tests: state with `destination=None` → returns `"clarify"`. State with all fields → returns `"search"`. LLM mocked in all router tests. 8 router unit tests pass. Ambiguous query "somewhere warm in December" → router returns `"clarify"`, no tool call made.

**Dev B — Clarification State & API Contract:**
- Extend `TripState` with `conversation_history: list[dict]` and `clarification_question: str | None`
- Update `POST /trips/{id}/plan` response: if graph returns a clarification question → `{"status": "clarification_needed", "question": "..."}` instead of `{"status": "planning_started"}`
- Add `POST /trips/{id}/clarify` endpoint: accepts `{"answer": "..."}`, appends to conversation history in Redis (keyed by `trip_id`), re-triggers the planning graph with updated state
- Unit test: submit ambiguous trip → receive clarification → submit answer → confirm graph runs to completion
- **Acceptance:** API correctly distinguishes `planning_started` vs `clarification_needed`. `POST /trips/{id}/clarify` re-runs the graph correctly. Conversation history preserved in Redis across both calls.

---

### Phase 8 — HotelAgent & ActivitiesAgent

**What this phase delivers:** All three agents work independently, each has a versioned prompt, each handles ambiguity via its own conditional edge.

---

**Dev A — HotelAgent:**
- Implement `HotelAgent` following Phase 6's exact pattern: single-node graph, `search_hotels` via MCP, `TripState` extended with `check_in`, `check_out`, `guests`, `hotels` (result)
- Start fresh — do NOT copy FlightAgent's system prompt. Hotels require different reasoning
- Conditional edge: required fields are `destination`, `check_in`, `check_out`, `budget_per_night` — any missing → clarify
- `prompts/hotel_agent_v1.md` written, tested on 5 cases, v2 created before phase is marked done
- **Acceptance:** `HotelAgent.run({"destination": "Goa", "check_in": "2025-12-10", "check_out": "2025-12-17", "budget_per_night": 5000, "guests": 2})` returns state with non-empty `hotels` list. 6 unit tests pass. Agent tested completely independently of OrchestratorAgent.

**Dev B — ActivitiesAgent:**
- Implement `ActivitiesAgent`: `get_attractions` via MCP, `TripState` extended with `interests`, `attractions` (result)
- "I like history and street food" must produce `AttractionInput(destination="Goa", interests=["history", "street food"], limit=5)` as a structured MCP call — this agent expects the most prompt iterations
- Conditional edge: required fields are `destination` and at least one interest — if interests absent → clarify ("What kinds of activities do you enjoy?")
- `prompts/activities_agent_v1.md` → minimum v2 before phase done; document what prompt changes fixed which failure modes
- Write a test injecting a non-English interest ("खाना") and document the behavior
- **Acceptance:** ActivitiesAgent tested independently. "I like history and street food in Goa" → `get_attractions` called with `interests=["history", "street food"]`. 6 unit tests pass. Non-English interest behavior documented.

---

### Phase 9 — Orchestrator: Decomposition & Fan-Out

**What this phase delivers:** A single user query fans out to all 3 sub-agents concurrently, progress events stream live via SSE, Orchestrator prompt is on v3+.

---

**Dev A — OrchestratorAgent Graph:**
- Build `OrchestratorAgent` in `src/ai/orchestrator/orchestrator.py` — a LangGraph graph with 3 nodes:
  1. `IntentParsingNode`: Gemini Flash extracts `destination`, `start_date`, `end_date`, `budget`, `group_size`, `interests` from free-form input into structured `TripState`. This is the hardest prompt in the system — document all failure modes
  2. `FanOutNode`: fires FlightAgent, HotelAgent, and ActivitiesAgent **concurrently** via LangGraph's parallel execution (`Send` API). Each sub-agent receives only its relevant state slice — not the full raw state
  3. `MergeNode`: collects results from all three sub-agents into unified `TripState`. Handles partial failures — one agent failed → log it, continue with what succeeded
- Orchestrator prompt must: define "ask vs. assume" policy explicitly for missing fields, normalise date formats, normalise currency
- `prompts/orchestrator_v1.md` written — must reach v3+ before phase is marked done
- **Acceptance:** Full run on "Plan a 7-day trip to Goa in December for 2 people, budget ₹50,000" → all 3 sub-agents called concurrently (verify via `agent_runs` — all 3 rows with overlapping `created_at` timestamps), results merged in `TripState`. Orchestrator prompt on v3+. 5 unit tests pass.

**Dev B — SSE Live Progress & Redis Pub/Sub Wiring:**
- Wire Redis pub/sub into each sub-agent's node: on completion, every agent publishes `{"agent": "flight_agent", "status": "completed", "summary": "Found 3 flights from DEL to GOI, cheapest ₹8,200"}` to `trip:{trip_id}:events`
- The SSE endpoint from Phase 5 already subscribes to this channel — no changes to the SSE route
- Add `"planning_started"` event published at the start of `POST /trips/{id}/plan` and `"planning_complete"` when the Orchestrator finishes
- Load test the SSE pipeline: 10 concurrent SSE connections to different trip streams, publish 50 events across them, confirm all 10 clients receive their respective events with zero cross-contamination
- **Acceptance:** SSE delivers real agent progress events in real time. 10-connection load test passes with zero event cross-contamination.

---

### Phase 10 — Budget Conflict & Re-Planning

**What this phase delivers:** The ₹40,000 Goa scenario works end-to-end, the re-planning branch is visible in LangGraph's visualisation, both routing unit tests pass.

---

**Dev A — Decision Node & Re-Planning Branch:**
- Add `BudgetDecisionNode` to the Orchestrator graph, running after `FlightAgent` returns and before `HotelAgent` is called:
  1. Call `estimate_budget` to project remaining budget after flights
  2. Evaluate: remaining budget after flights < minimum viable hotel + activity spend → `"escalate"` | borderline → `"replan"` | comfortable → `"continue"`
  3. Produce structured `BudgetDecision(decision: "replan" | "escalate" | "continue", reason: str, remaining_budget: float)` via Pydantic output parsing — never freeform string
- Re-plan branch: call FlightAgent again with cheaper params (`max_stops=1`, lower budget). Cap re-plan attempts at 2 — after that, escalate regardless
- Escalate branch: return `{"status": "budget_conflict", "reason": "...", "options": [...]}` to user
- **Acceptance:** Scenario "5 days Goa, ₹40,000 budget, flights ₹28,000" → routes to `"escalate"` or `"replan"`. Unit test: mock FlightAgent at 70% of budget → `"escalate"`. Mock at 40% → `"continue"`. Both pass with LLM mocked. LangGraph visualisation shows the branch.

**Dev B — Budget Conflict State & API Response:**
- Extend `TripState` with `budget_decision: BudgetDecision | None`, `replan_attempts: int`
- Budget conflict → SSE event `{"event": "budget_conflict", "reason": "...", "options": [...]}` published to the trip's Redis channel in real time
- Add `POST /trips/{id}/replan` endpoint: accepts `{"choice": "cheaper_flights" | "reduce_days" | "increase_budget"}`, re-triggers planning with the chosen adjustment
- Persist `budget_decision` JSON into the `agent_runs` row for the decision node
- **Acceptance:** Budget conflict event arrives via SSE before `planning_complete`. `POST /trips/{id}/replan` re-runs correctly. Decision node output persisted in `agent_runs`.

---

### Phase 11 — Evaluator Agent: Self-Checking

**What this phase delivers:** A deliberately broken itinerary is caught by the Evaluator, the system loops, corrects, and the final output is clean.

---

**Dev A — Evaluator Agent:**
- Build `EvaluatorAgent` in `src/ai/agents/evaluator.py` using Claude Haiku 4.5 — a separate LLM gives a genuine independent check
- Evaluator checks the draft itinerary before ItineraryBuilder runs:
  1. Do activity dates fall inside the travel window?
  2. Do flights + hotels + daily spend match `estimate_budget` within 5%?
  3. Are any activities duplicated on the same day?
  4. Are any referenced activities NOT in the `get_attractions` response (hallucination check)?
- On fail: return `EvaluatorVerdict(passed: bool, failures: list[EvaluatorFailure], retry_count: int)` — structured Pydantic output, never freeform
- On fail: log the specific reason, loop back to the relevant agent. Cap retries at 3. After 3, mark the trip `"failed"` with an honest error
- `prompts/evaluator_v1.md` written; document each failure type
- **Acceptance:** 4 known-bad itinerary fixtures (one per failure type) all caught with correct failure type in `EvaluatorVerdict.failures`. Retry loop triggers and re-runs the correct agent. After 3 retries → `"failed"`. All 4 tests pass.

**Dev B — Evaluator Run Logging & Retry Tracking:**
- Wire `EvaluatorVerdict` into `agent_runs`: `agent_name = "evaluator"`, `output = verdict.model_dump()`, `status = "completed"` or `"failed"`
- Add `retry_count` tracking to `TripState` and persist it — `GET /trips/{id}/runs` reveals exactly how many retries occurred and why
- Build `get_retry_chain(trip_id)` that queries `agent_runs` and reconstructs the evaluation + retry timeline as an ordered list
- Test: run full pipeline with a known-bad input, query `GET /trips/{id}/runs`, confirm evaluator row appears between agent rows and retry is visible
- **Acceptance:** Evaluator verdict queryable via `GET /trips/{id}/runs`. `get_retry_chain(trip_id)` returns correct ordered timeline. Retry count never exceeds 3 (hard cap tested).

---

### Phase 12 — Itinerary Builder: Claude Haiku + Structured Synthesis

**What this phase delivers:** Builder produces correctly structured JSON, all referenced activities are from actual data, budget math is consistent, prompt is on v3+.

---

**Dev A — ItineraryBuilder:**
- Build `ItineraryBuilder` in `src/ai/builder/builder.py` using Claude Haiku 4.5
- Receives Evaluator-approved data from all three agents. Produces **structured day-by-day JSON** — not markdown prose. Define the exact schema in the prompt with field names, types, and an example day:
  ```json
  {
    "days": [
      {
        "day": 1, "date": "2025-12-10",
        "morning": {"activity": "...", "cost": 0, "lat": 15.5, "lng": 73.8},
        "afternoon": {"activity": "...", "cost": 0, "lat": null, "lng": null},
        "evening": {"activity": "...", "cost": 0, "lat": null, "lng": null},
        "hotel": {"name": "...", "cost_per_night": 4500},
        "flight": null
      }
    ],
    "total_cost": 48500,
    "currency": "INR"
  }
  ```
- Data scope enforced in prompt: only reference flights/hotels/activities from provided data. Test by removing one activity from input and confirming it does NOT appear in output
- Fallback handling defined in the prompt: if `get_weather` returned error → omit weather from that day's description; if `get_attractions` returned empty → note "explore the area"
- `prompts/itinerary_builder_v1.md` → must reach v3 before phase done
- **Acceptance:** Builder output validates against the JSON schema. Budget math: `sum(day costs)` within ₹500 of `total_cost`. No activity in output that wasn't in input data. Prompt on v3+. 5 tests pass.

**Dev B — Builder Output Persistence:**
- After Builder produces JSON: write `itineraries` row with `structured_data = <builder JSON>`, `total_cost` extracted from JSON
- Trigger embedding stub `generate_embeddings(itinerary_id)` (fully implemented in Phase 14 — stub logs "embedding pending" for now)
- Update `trips.status = "completed"` on success, `"failed"` on Builder error
- All writes in one SQLAlchemy transaction: itinerary row + trip status update atomically
- **Acceptance:** Full pipeline run → `itineraries` table has one row. `trips.status = "completed"`. `GET /trips/{id}/itinerary` returns the structured JSON (was 404 before). Forced mid-write failure → neither row persists (rollback tested).

---

### Phase 13 — Persistence: Storing Every Run

**What this phase delivers:** Full pipeline run → all tables populated → `GET /trips/{id}/runs` returns a complete, correctly ordered trace of every agent decision.

---

**Dev A — Agent Run Instrumentation Audit:**
- Audit every agent and node to confirm `log_agent_run` is called correctly:
  - FlightAgent: input = `{origin, destination, date, budget, passengers}`, output = `{flights: [...]}` or `{error: ...}`, duration_ms measured
  - HotelAgent: same pattern with hotel fields
  - ActivitiesAgent: same pattern with interest/attraction fields
  - OrchestratorAgent IntentParsingNode: input = raw user message, output = extracted structured fields
  - BudgetDecisionNode: input = flight cost + remaining budget, output = `BudgetDecision`
  - EvaluatorAgent: input = draft itinerary data, output = `EvaluatorVerdict`
  - ItineraryBuilder: input = all agent outputs (store shape, not full content), output = summary stats
- Fix any gaps found — every node that makes a decision must have an `agent_runs` row. Silent nodes are invisible bugs
- **Acceptance:** Full pipeline produces at least 7 `agent_runs` rows. `GET /trips/{id}/runs` returns them ordered by `created_at` ascending. Every row has non-null `duration_ms`. A re-planned trip has additional rows for the re-plan cycle.

**Dev B — Trip Status Lifecycle & Query Endpoints:**
- Confirm complete `trips.status` lifecycle: `pending → planning → completed | failed`
- `GET /trips` returns authenticated user's trips with `status` correctly reflecting current state (not stale)
- Build `GET /trips/{id}/timeline` (internal debugging endpoint): returns `agent_runs` joined with `itineraries` as a single ordered event log with human-readable labels
- Add `GET /trips` filter: `?status=completed` and `?status=failed` — useful for frontend's "past trips" view and for debugging
- **Acceptance:** Status lifecycle correct across all branches (happy path, budget conflict, evaluator retry, failure). `GET /trips?status=completed` returns only completed trips. `GET /trips/{id}/timeline` returns correctly ordered event log.

---

### Phase 14 — Embeddings: Write-Time Vector Storage

**What this phase delivers:** Every itinerary write triggers an embedding insert, both embedding versions exist, you can `SELECT` a vector from psql and confirm correct dimension.

---

**Dev A — Embedding Generation:**
- Replace the Phase 12 stub with real embedding generation in `src/ai/embeddings/embedder.py`
- Use OpenAI `text-embedding-3-small` (1536 dimensions) — consistent with Phase 4 schema
- Generate **two embeddings per itinerary**:
  1. Full text embedding: concatenate all day descriptions from `structured_data` into one string, embed it
  2. Structured summary embedding: `"{destination} {duration} days {budget_range} INR. Top activities: {top_5_activity_names}"` — better similarity signal for search
- Store both rows in `embeddings` table with `embedding_model = "text-embedding-3-small"` and correct `itinerary_id` FK
- Add `tenacity` exponential backoff around the embed call (OpenAI rate limit: 3,000 RPM)
- **Acceptance:** After full pipeline run, `SELECT count(*) FROM embeddings WHERE itinerary_id = '...'` returns 2. `SELECT array_length(vector, 1) FROM embeddings` returns 1536 for both. Correct `embedding_model` label stored.

**Dev B — Embedding Write Pipeline & Failure Handling:**
- Wire embedding generation as a FastAPI `BackgroundTask` after itinerary save — HTTP response is never blocked by the OpenAI API call
- Add `embeddings_pending` counter in Redis (increment on save, decrement on success/failure) — surfaced at `GET /admin/embedding-health`
- On OpenAI failure: log the error, write an `embeddings` row with `embedding_model = "pending_retry"` and null vector — system remains functional, similarity search degrades gracefully
- On app startup: query for `pending_retry` rows and re-queue them as background tasks (recovery from killed process)
- **Acceptance:** HTTP response not delayed by embedding generation. OpenAI failure → no uncaught exception, trip marked completed, `pending_retry` row present. Startup recovery re-queues pending rows.

---

### Phase 15 — Conversation Memory: Multi-Turn Refinement

**What this phase delivers:** "Change hotels to something closer to the beach" → only HotelAgent re-runs → new itinerary with same flights and activities unchanged.

---

**Dev A — Refinement Classifier & Selective Re-Run:**
- Add `RefinementClassifierNode` at the start of the Orchestrator graph, active on turn 2+:
  - Receives full conversation history + new user message
  - Classifies via Gemini Flash with structured Pydantic enum output: `"full_replan"` | `"targeted_hotel"` | `"targeted_flights"` | `"targeted_activities"` | `"add_day"` (all agents)
- Selective re-run: on `"targeted_hotel"`, only HotelAgent runs — FlightAgent and ActivitiesAgent results from previous turn carried forward in `TripState` unchanged
- Hard cases defined explicitly in the prompt and documented in `DECISIONS.md`:
  - "Make it cheaper" → `"targeted_flights"` first, then budget decision node
  - "Add a day" → `"add_day"` → all three agents
  - "I'd rather go to Mumbai" → `"full_replan"` → all agents, reset state
- `prompts/refinement_classifier_v1.md` written
- **Acceptance:** "Change hotels to beach-view" → only HotelAgent re-runs (verify: only one new `agent_runs` row, `agent_name = "hotel_agent"`). Flights from previous turn preserved unchanged. 5 classifier unit tests pass (one per classification type, LLM mocked). All 3 hard-case policies documented and tested.

**Dev B — Conversation History Storage & State Versioning:**
- Store conversation history in Redis: `trip:{trip_id}:history` → list of `{role, content, turn}` dicts, appended on each turn
- On selective re-run: write a new `itineraries` row (don't overwrite — old itinerary is history) and new `agent_runs` rows with incremented `turn` field
- Add `turn` column to `agent_runs` table via Alembic migration; `GET /trips/{id}/runs?turn=2` returns only second turn's runs
- `GET /trips/{id}/itinerary` returns latest itinerary (most recent `created_at`); `GET /trips/{id}/itineraries` returns all versions
- **Acceptance:** Two-turn conversation produces 2 `itineraries` rows and correctly labelled `agent_runs` with `turn` field. `GET /trips/{id}/itineraries` returns both, ordered newest first. Redis history survives across HTTP requests. Migration runs cleanly.

---

### Phase 16 — User Preferences & Personalisation

**What this phase delivers:** User with "vegetarian" preference → activity recommendations default to vegetarian-friendly without being asked. Home city never asked for after the first trip.

---

**Dev A — Preference Injection into Prompts:**
- Add `user_preferences` table via Alembic migration: `user_id` (FK), `dietary_restrictions: list[str]`, `preferred_airlines: list[str]`, `travel_style: str` ("budget" | "mid-range" | "luxury"), `home_city: str`, `updated_at`
- Load preferences at the start of every Orchestrator run and inject into each sub-agent's system prompt:
  - FlightAgent: `preferred_airlines` → added to search params; `home_city` → default origin if not specified
  - ActivitiesAgent: `dietary_restrictions` → added to `interests`; `travel_style` → adjusts activity categories
  - ItineraryBuilder: all preferences injected as context
- Preference injection is silent — never ask for something already provided
- **Acceptance:** User with `dietary_restrictions = ["vegetarian"]` → ActivitiesAgent MCP call includes "vegetarian" in interests without being asked. `home_city = "Delhi"` → FlightAgent uses DEL as origin on second trip. 4 unit tests pass.

**Dev B — Preference Extraction & Endpoints:**
- `PreferenceExtractorNode`: after each completed trip, Claude Haiku 4.5 reads the finalised itinerary and updates `user_preferences` additively (infers `travel_style` from hotel star ratings and total spend; never removes explicitly set preferences)
- `PUT /users/preferences` endpoint: explicit preference overwrite; `GET /users/preferences` returns current preferences
- **Acceptance:** `PreferenceExtractorNode` correctly updates `travel_style` after a luxury trip. `PUT /users/preferences` overwrites correctly. Preferences persist across sessions (DB-backed). 3 tests pass.

---

### Phase 17 — Frontend: Chat Interface & SSE Streaming

**What this phase delivers:** Full flow works in browser, SSE stream visible in UI, agent progress panel updates live.

---

**Dev A — Backend API Hardening for Frontend:**
- All API responses frontend-ready: `datetime` as ISO 8601 UTC strings, `UUID` as strings, all `ToolError` paths return `{"error": {"code": "...", "message": "..."}}` envelope
- Add `GET /trips/{id}/status` endpoint (SSE polling fallback): returns `{"status": "...", "progress": {"agents_done": 2, "agents_total": 3}}`
- Confirm CORS works with Next.js dev server at `localhost:3000`; document production CORS policy in `DECISIONS.md`
- **Acceptance:** All endpoints return correctly shaped responses. CORS correct for `localhost:3000`. `GET /trips/{id}/status` returns coherent progress object during active planning.

**Dev B — Next.js Frontend:**
- Scaffold Next.js 14 app in `src/frontend/` with Tailwind CSS and shadcn/ui
- **Chat view:** text input, submit button, message thread. On submit: `POST /trips` then `POST /trips/{id}/plan`, open SSE stream, render incoming events as progress indicators
- **Agent progress panel:** live sidebar with status badges (pending → running → done/failed) per agent — Flights, Hotels, Activities — updated from SSE events in real time
- **Itinerary view:** after `"planning_complete"` SSE event, fetch `GET /trips/{id}/itinerary` and render `structured_data` as day-by-day cards (day number, date, morning/afternoon/evening slots, hotel name + cost, total day cost)
- **SSE lifecycle:** reconnection on dropped connection via `EventSource` `onerror` + exponential backoff; partial state preserved on backend crash (show last known agent status, not blank screen)
- One **Playwright E2E test**: type "Plan a 5-day trip to Goa in December for 2 people, budget ₹50000" → wait for SSE `planning_complete` → assert itinerary cards render. This is the smoke test for every future change
- **Acceptance:** Full flow works in browser end to end. Agent progress panel updates in real time. Itinerary renders as day-by-day cards. Playwright E2E test passes. SSE reconnects after network interruption.

---

## Chapter 3 — Storage, Intelligence & Frontend Polish (Phases 18–25)

### Phase 18 — Map View

**What this phase delivers:** Every itinerary shows attraction pins on a map, route drawn per day, clicking a pin shows details.

---

**Dev A — Coordinate Validation & Builder Integration:**
- Confirm `Attraction.lat` and `Attraction.lng` are always populated from Phase 3's `get_attractions` — no frontend geocoding needed (cleaner architecture, decided in Phase 3)
- Update `ItineraryBuilder` prompt to always include `lat`, `lng` from attraction data in output JSON
- Add validation in the itinerary save path: log a warning (don't fail) if any activity slot is missing coordinates — surfaced as a metric later
- **Acceptance:** 95%+ of activity slots in persisted itineraries have non-null `lat`/`lng`. Warning fires correctly for null-coordinate activities.

**Dev B — Leaflet Map Component:**
- Add `leaflet` and `react-leaflet` to the frontend
- **Map component** rendered below day-by-day cards: activity pins colour-coded by day (day 1 = blue, day 2 = green, etc.), hotel pin in gold, flight origin/destination as info markers
- Day-by-day route: `Polyline` connecting the day's activity coordinates in order (morning → afternoon → evening)
- Pin click → popup card: activity name, category, rating, cost estimate, time slot
- Missing coordinates → text note in card, map does not crash
- **Acceptance:** Map renders with correct pins for a real itinerary. Pin click shows detail popup. Polyline connects the day's activities in order. Different days have different pin colours. Missing coordinates don't crash the map.

---

### Phase 19 — PDF Export

**What this phase delivers:** PDF downloads from the frontend, contains all itinerary content, includes a static map image.

---

**Dev A — PDF Generation Endpoint:**
- Server-side PDF generation using `weasyprint` (or `reportlab` if weasyprint has CSS rendering issues — decide and document in `DECISIONS.md`)
- `GET /trips/{id}/export/pdf` → streams PDF (`Content-Type: application/pdf`, `Content-Disposition: attachment; filename=trip-{destination}-{date}.pdf`)
- PDF structure: cover page (destination, dates, total cost), day-by-day pages (morning/afternoon/evening + hotel + day cost), cost breakdown page (flights + hotels + activities), static map image (Google Maps Static API with activity coordinates as markers)
- Static API failure → omit map image, log warning, PDF still downloads
- **Acceptance:** PDF downloads successfully. Contains all 4 sections. Static map image appears for a real itinerary. Static API failure → PDF still downloads without the map.

**Dev B — Frontend PDF Download:**
- "Download PDF" button on itinerary view; `onClick` → fetch `GET /trips/{id}/export/pdf`, create blob URL, trigger download
- Loading state visible during generation (1–3 seconds)
- Error state: non-200 response → toast notification "PDF generation failed — try again"
- **Acceptance:** Button triggers a real PDF download. Loading state visible. Error state shows toast.

---

### Phase 20 — Frontend Polish

**What this phase delivers:** Streaming text works, refinement flow has visual feedback, error states handled, mobile-responsive layout.

---

**Dev A — Token-by-Token Streaming:**
- Extend `ItineraryBuilder` to stream output token-by-token via SSE: new event type `"builder_token"` with `{"token": "..."}` — frontend accumulates these into a live markdown preview
- Builder still validates the final JSON before writing to DB — stream tokens, validate complete output
- **Acceptance:** Itinerary text appears token-by-token in frontend during builder execution. DB write only happens on complete, valid JSON.

**Dev B — Refinement UI & Error States:**
- Refinement flow: after itinerary displayed, show chat input pre-filled "Refine this trip:" — on targeted re-run, only the changed section animates/updates; flights and activities stay static
- Visual diff: when selective re-run completes, highlight changed sections with a brief colour flash (green)
- Error states: agent failure → show which agent failed and a "Retry" button; budget conflict → show conflict options (cheaper flights / reduce days / increase budget) as tappable cards
- Mobile responsive: all views render without horizontal scroll at 375px width; agent progress panel collapses to a bottom sheet on mobile
- **Acceptance:** Refinement correctly animates only the changed section. Error states render correctly. All views work at 375px. Playwright test for refinement flow passes.

---

### Phase 21 — Smarter Budget Intelligence

**What this phase delivers:** Budget estimate includes seasonal adjustment, a confidence range, and the Goa/₹40k scenario shows 3 concrete cheaper alternatives.

---

**Dev A — Seasonal Pricing & Confidence Range:**
- Extend `estimate_budget` MCP tool with seasonal multipliers: inject current month and destination (Goa December ≈ 1.4× Goa July pricing)
- Output a confidence range: `BudgetEstimate.total_min`, `BudgetEstimate.total_max` (±20% based on destination/season price volatility)
- Evaluator from Phase 11 uses this range: itinerary total must fall within `[total_min, total_max]` — if outside, it's a budget inconsistency failure
- **Acceptance:** `estimate_budget(flights=8000, hotels=3000, days=5, daily_spend=2000, destination="Goa", month=12)` returns a range, not just a total. Evaluator rejects itinerary whose total falls outside the range. 3 unit tests pass.

**Dev B — Alternatives in Budget Conflict:**
- When `BudgetDecisionNode` escalates, generate 3 concrete alternatives using `estimate_budget` re-runs:
  1. "Switch to a 3-star hotel → saves ₹12,000"
  2. "Reduce trip to 5 days → total ₹38,500"
  3. "Travel in off-peak month → saves ₹8,000 on flights"
- Alternatives arrive via SSE as `{"event": "budget_alternatives", "options": [...]}` with concrete INR amounts
- **Acceptance:** Budget conflict SSE event includes 3 alternatives with concrete amounts. Generated without additional MCP calls (arithmetic reuse of existing data). Tested against a known conflict scenario.

---

### Phase 22 — Destination Intelligence Agent

**What this phase delivers:** Every itinerary has a "Local Intelligence" section with destination-specific advice that no API can provide.

---

**Dev A — DestinationIntelligenceAgent:**
- Fourth agent running in parallel with the existing three. Claude Haiku 4.5 with training knowledge — no MCP tool calls
- Output structure:
  ```json
  {
    "local_transport": "Rent a scooter for ₹400/day — best way to explore Goa",
    "cultural_norms": ["Dress modestly at temples", "Bargaining expected at markets"],
    "tourist_traps": ["Avoid restaurants with menus in 10 languages near Calangute beach"],
    "best_times": {"Fort Aguada": "Early morning before 9am — deserted"},
    "safety_tips": ["Don't leave valuables on the beach"]
  }
  ```
- Wire into OrchestratorAgent's fan-out — runs in parallel with the other three
- ItineraryBuilder receives this output and incorporates it into `structured_data` under `"local_intelligence"` key
- **Acceptance:** Agent runs in parallel (verify: overlapping `created_at` in `agent_runs`). Output present in `structured_data`. Zero MCP tool calls (verify via `agent_runs` input/output). 4 unit tests pass.

**Dev B — Local Intelligence in Frontend:**
- "Local Tips" accordion section in itinerary view, rendered below day-by-day cards: Local Transport, Cultural Norms, Tourist Traps, Best Times — collapsed by default, expanded on click using shadcn/ui Accordion
- If DestinationIntelligenceAgent failed: section simply absent (graceful degradation, no error shown)
- **Acceptance:** Local Tips renders correctly for destinations where agent returned data. Gracefully absent on agent failure. Accordion opens/closes correctly.

---

### Phase 23 — pgvector Similarity Search

**What this phase delivers:** `GET /trips/{id}/similar` returns sensibly related trips. Embedding strategy documented and justified. Was 501 since Phase 5.

---

**Dev A — Similarity Search Implementation:**
- Implement `GET /trips/{id}/similar` — returns up to 5 similar past itineraries using pgvector cosine similarity search against the HNSW index from Phase 4
- Query uses the current trip's structured summary embedding (not full-text — test both and document which gives better results in `DECISIONS.md`)
- Run embedding experiments with at least 3 test queries; document which strategy produces better similarity and why
- Document the HNSW vs. IVFFlat choice in `DECISIONS.md`: HNSW gives better recall at the cost of more memory — correct trade-off for this use case
- **Acceptance:** `GET /trips/{id}/similar` returns 5 results with similarity scores. A Goa beach trip is similar to another beach trip, not a Ladakh trek. Embedding strategy documented.

**Dev B — Similarity Search Frontend & Text Search:**
- "Similar Trips" section in itinerary view: 3 past itinerary thumbnails (destination, dates, total cost, one activity highlight). Clicking opens that itinerary
- Add `GET /trips/search?q=beach+under+50k+5+days` endpoint: embeds the query string on the fly, runs pgvector search against structured-summary embeddings, returns matching trips
- Frontend search input on the trips list page
- **Acceptance:** "Similar Trips" section renders with correct data. `GET /trips/search?q=...` returns sensible results. Frontend search input works end to end.

---

### Phase 24 — Caching & Rate Limit Handling

**What this phase delivers:** Differentiated TTLs verified via Redis CLI. Rate limit scenario handled gracefully. Backoff tested with a forced 429.

---

**Dev A — Cache Warming & Rate Limit Queueing:**
- Cache warming: when a trip is created (`POST /trips`), immediately pre-warm the weather and flight search caches for that destination+date in a background task — by the time planning triggers, the first MCP call is often a cache hit
- Rate limiter in `src/ai/mcp_client/rate_limiter.py`: tracks calls per API per minute (Amadeus: 60/min, Google Maps: 100/min, OWM: 60/min). When a call would exceed the limit, queue it and wait rather than failing
- Test 3 concurrent agents all calling Amadeus simultaneously — the rate limiter serialises excess calls, zero raw 429s reach MCP tool functions
- **Acceptance:** Cache warming fires on `POST /trips` (visible in logs: `[CACHE WARM] flights:...`). Zero raw 429s. Forced 429 from mock Amadeus → caught, queued, retried after backoff. All 3 concurrent agents complete.

**Dev B — Exponential Backoff & TTL Verification:**
- Add `tenacity` exponential backoff with jitter around all external API calls: `wait_exponential_jitter(initial=1, max=30)`, `stop_after_attempt(4)`
- Test: force 429 from mock Amadeus client → confirm backoff fires with roughly doubling wait times + jitter (assert on logged wait times)
- Verify all TTLs via Redis CLI: `TTL mcp:flights:*` → ~300. `TTL mcp:attractions:*` → ~21600. Document any discrepancies vs. Phase 3 spec
- **Acceptance:** Backoff fires on forced 429 with correct wait times logged. All 4 TTL values verified against Phase 3 spec. Discrepancies corrected and documented.

---

### Phase 25 — Group Trip Intelligence

**What this phase delivers:** A 4-person group trip with varied interests produces an itinerary that balances preferences. Per-person cost breakdown in the output.

---

**Dev A — Multi-Objective ActivitiesAgent:**
- Extend `TripState` with `group_members: list[{"name": str, "interests": list[str]}]`
- ActivitiesAgent updated: call `get_attractions` once per member's interests, merge results, de-duplicate, rank by average relevance across all members. Scoring function: `score_activity_for_group(activity, members) -> float`
- ItineraryBuilder updated: day slots balance across member profiles — not all activities for one person
- **Acceptance:** 4-person group [beach/food] + [history/culture] + [adventure] + [spa/relaxation] → at least one activity per member type per 2 days. No single member's interests dominate all slots. 3 unit tests pass.

**Dev B — Per-Person Cost Breakdown:**
- `estimate_budget` extended: when `group_size > 1`, output includes `per_person_breakdown` with individual cost shares
- `structured_data` JSON includes per-person costs at the top level
- Frontend: show "₹12,500/person" prominently in the itinerary header alongside total cost
- **Acceptance:** Group trip itinerary shows per-person breakdown. `total_cost / group_size ≈ per_person_cost` within ₹100 arithmetic. Frontend renders correctly.

---

## Chapter 4 — Production (Phases 26–34)

### Phase 26 — LangSmith Tracing

**What this phase delivers:** Every agent run produces a complete trace. Re-planning and evaluation branches visible. Filterable by `trip_id`.

---

**Dev A — LangSmith Integration:**
- Add LangSmith tracing: `LANGCHAIN_TRACING_V2=true`, `LANGCHAIN_API_KEY` in `.env`
- Every LangGraph node execution is a span: input, output, duration, token count
- Tag every trace with `trip_id` and `user_id` as metadata — filter by `trip_id` in LangSmith UI to see every decision for that specific trip
- Re-planning branches appear as distinct spans; EvaluatorAgent retry loops appear as numbered spans
- **Acceptance:** One planning run produces a complete trace in LangSmith. Re-planning branch appears as distinct span. Evaluator retries appear as numbered spans. Filtering by `trip_id` returns only that trip's traces.

**Dev B — Token Cost Tracking:**
- Extend `agent_runs` with `token_count: int | None` and `cost_usd: float | None` columns (Alembic migration)
- Extract token usage from LangChain callback and write to DB alongside LangSmith
- `GET /admin/cost-summary?since=2025-01-01`: total tokens and USD cost grouped by agent and by date
- Alert threshold: single trip total LLM cost exceeds $0.10 → log warning
- **Acceptance:** `agent_runs.token_count` populated after each run. `GET /admin/cost-summary` returns correct aggregate. Over-cost alert fires in a test simulating 10 evaluator retries.

---

### Phase 27 — Production Infrastructure

**What this phase delivers:** Full pipeline runs against production infrastructure end-to-end. Sentry catches a test error.

---

**Dev A — Backend Production Deploy:**
- Backend → Railway: Dockerfile deploy; document every missing env var found on first deploy in `DECISIONS.md`
- Sentry: `sentry_sdk.init(dsn=settings.SENTRY_DSN, traces_sample_rate=0.1)`; deliberately raise an exception on a test endpoint and verify it appears in Sentry
- Verify pgvector extension on Neon: `SELECT * FROM pg_extension WHERE extname = 'vector'`
- **Acceptance:** Backend accessible at Railway URL. One deliberate test error appears in Sentry. pgvector confirmed on Neon.

**Dev B — Frontend & Redis Production Deploy:**
- Frontend → Vercel: check function timeout — planning takes 15–30s, default 10s will fail. Use streaming or upgrade timeout
- Redis → Upstash: Upstash REST API does NOT support pub/sub — must use TCP connection. Document this in `DECISIONS.md`
- Run full planning flow against production end-to-end: Vercel → Railway → Neon + Upstash → real APIs
- **Acceptance:** Full end-to-end flow works in production. SSE streaming works via Upstash TCP (not REST). Vercel function timeout not hit.

---

### Phase 28 — CI/CD Pipeline

**What this phase delivers:** Push to feature branch → lint + tests in under 3 minutes. Merge to main → auto-deploy. Nightly job runs and posts results.

---

**Dev A — CI Pipeline:**
- GitHub Actions: on every push — `ruff check src/` (lint), `pytest tests/unit/ tests/contract/ -v` (no network)
- A deliberately broken test in a PR must fail the pipeline and block merge — test this before marking the phase done
- `alembic check` step: fails the build if DB schema is ahead of migrations
- **Acceptance:** Lint catches a ruff violation. Tests run in under 2 minutes. `alembic check` step works. Broken test blocks merge.

**Dev B — CD Pipeline & Nightly:**
- CD on merge to main: GitHub Actions → `railway up` + Vercel auto-deploy via GitHub integration; verify via `railway status`
- Nightly job (`0 2 * * *`): `RUN_INTEGRATION=1 pytest tests/integration/ -v` + eval suite from Phase 33 + posts summary to a dedicated GitHub issue via `gh issue comment`
- **Acceptance:** Merge to main → automatic Railway + Vercel deploy within 5 minutes. Nightly job posts results to GitHub issue.

---

### Phase 29 — Observability: Prometheus, Grafana & OpenTelemetry

**What this phase delivers:** RED metrics dashboard live. One planning request produces a single connected trace from API entry to DB write. Log lines searchable by `trip_id`.

---

**Dev A — Prometheus Metrics:**
- `prometheus-fastapi-instrumentator` wired into FastAPI app: `Instrumentator().instrument(app).expose(app)`
- Custom metrics: `llm_calls_total` (counter, labelled by agent and model), `mcp_cache_hit_ratio` (gauge), `itinerary_builds_total` (counter, labelled by status: success/failure/retry)
- **Acceptance:** Grafana dashboard shows live request rate/error rate/duration per endpoint. Custom LLM and cache metrics visible.

**Dev B — OpenTelemetry Distributed Tracing & Structured Logging:**
- `opentelemetry-instrumentation-fastapi` and `opentelemetry-instrumentation-sqlalchemy`; Jaeger or Zipkin backend
- `trip_id` injected as a span attribute at FastAPI middleware level; every downstream call (DB, Redis, MCP) inherits the trace
- Structured logging: `python-json-logger`; `trip_id` and `user_id` on every log line via `logging.Filter` reading from `contextvars`
- **Acceptance:** One planning request produces a single connected trace spanning FastAPI → SQLAlchemy → Redis → MCP. Searching by `trip_id` in the logging UI returns log lines from every service touched. No orphaned spans.

---

### Phase 30 — Load Testing & Performance Baseline

**What this phase delivers:** A documented baseline report exists. Bottleneck identified and fixed, before/after numbers recorded.

---

**Dev A — Load Test Scripts & Baseline:**
- k6 load test scripts: `POST /trips` + `POST /trips/{id}/plan` + `GET /trips/{id}/stream` (SSE) + `GET /trips/{id}/itinerary` — realistic mix of concurrent users
- Run initial load test against staging, capture baseline (req/s, p50/p95/p99, error rate) — watch Grafana live during the run
- **Acceptance:** Documented baseline report with real numbers before any tuning. k6 scripts run cleanly against staging.

**Dev B — Bottleneck Diagnosis & Tuning:**
- Diagnose the actual bottleneck with Dev A: likely candidates are SQLAlchemy pool exhaustion (`pool_size`/`max_overflow`), uvicorn worker count (GIL → throughput scaling is about process count, not thread count within one process), or Redis connection pool
- Write the hypothesis before applying the fix; tune; re-run the load test
- Document: "before: X req/s at Yms p99, bottleneck was Z; after: A req/s at Bms p99" in `docs/load-test-report.md`
- **Acceptance:** Hypothesis documented before the change. Before/after comparison with real numbers. At least one specific configuration change made and justified.

---

## Chapter 5 — Intelligence Layer & Chaos (Phases 31–44)

### Phase 31 — Chaos Engineering: Agent Failures

**What this phase delivers:** Kill an agent mid-run — system degrades gracefully, recovers, no data corruption.

---

**Dev A — Agent Failure Modes:**
- Chaos exercise 1: kill the MCP server process mid-planning — expected: `ToolError(code="CONNECTION_REFUSED")` propagates, Orchestrator marks the affected agent `"failed"`, continues with partial results (clearly labelled in itinerary)
- Chaos exercise 2: force Gemini Flash API to return a 429 — does OrchestratorAgent backoff correctly, or fail the entire trip?
- Both documented: hypothesis written before running, actual behavior recorded, any fixes noted
- **Acceptance:** MCP server kill → graceful degradation, `agent_runs` shows `status = "failed"` with error reason. Gemini 429 → backoff fires, run completes or escalates gracefully. No data corruption in either case.

**Dev B — LLM Failure & SSE Pipeline Chaos:**
- Chaos exercise 3: Claude Haiku times out during ItineraryBuilder — trip must time out and mark `"failed"` within 60s (not hung indefinitely)
- Chaos exercise 4: kill Redis for 30 seconds during active SSE streams — do clients reconnect? Do orphaned pubsub subscriptions remain?
- **Acceptance:** Builder timeout → trip marked `"failed"` within 60s. Redis kill → clients reconnect via `EventSource` backoff. No orphaned Redis pubsub subscriptions after restart (verify with `CLIENT LIST`).

---

### Phase 32 — Chaos Engineering: Data Layer

**What this phase delivers:** Postgres failover scenario documented. Embedding queue drains correctly after recovery.

---

**Dev A — Database Chaos:**
- Chaos exercise 5: kill Postgres container mid-itinerary write — does `pool_pre_ping` detect the dead connection and reconnect?
- Test the atomic transaction from Phase 12: kill Postgres after `itineraries` row inserts but before `trips.status` updates — confirm full rollback (no half-committed state)
- **Acceptance:** Postgres kill → app reconnects within 10s. Half-committed scenario → full rollback confirmed by querying both tables.

**Dev B — Embedding Queue Recovery:**
- Chaos exercise 6: kill the FastAPI process immediately after an itinerary is written but before the background embedding task starts — does the embedding get generated on next start, or silently lost?
- Confirm recovery: on startup, `pending_retry` rows re-queued as background tasks (Phase 14's pattern applied)
- **Acceptance:** Embedding queue drains correctly after restart. No permanently-lost embeddings. All `pending_retry` rows retried on next startup. Confirmed by checking `embeddings` table after forced kill + restart.

---

### Phase 33 — Eval Suite

**What this phase delivers:** Eval suite runs automatically, produces a score table, and catches a regression when run against an intentionally broken version.

---

**Dev A — Test Scenarios & Grader:**
- 15 test scenarios: happy path (3), budget conflict (2), ambiguous query (2), group trip (2), edge cases (4: very low budget, unusual destination, max group size, past date), refinement flow (2)
- LLM-based grader using Claude Haiku 4.5 scoring each output on:
  - Tool correctness: right tools called with right params (check `agent_runs`)
  - Budget consistency: `total_cost` in itinerary matches `estimate_budget` within 5%
  - Hallucination check: any itinerary content not in source MCP data
  - Completeness: every requested day has morning/afternoon/evening slots
  - Refinement accuracy: only the correct sections changed on a targeted refinement
- Grader outputs a score table: scenario name, score (0–5), specific failure reasons
- **Acceptance:** All 15 scenarios run without crashing. Score table produced. Intentionally broken version → score drops and is detected. Suite runs in under 10 minutes.

**Dev B — Eval Runner & Reporting:**
- `scripts/run_eval.py`: runs all 15 scenarios, outputs Markdown score table to `docs/eval-results-{date}.md`
- `--assert-min-score 3.5` flag: exits with code 1 if average score drops below threshold — used in nightly CI from Phase 28
- Append each run's average score to `docs/eval-history.csv` — trend over time to detect gradual regressions
- **Acceptance:** `python scripts/run_eval.py --assert-min-score 3.5` exits 0 on a healthy build, exits 1 on a broken one. `eval-history.csv` grows correctly after each run.

---

### Phase 34 — Advanced Monitoring & Alerts

**What this phase delivers:** Alerts fire on real anomalies. On-call runbook exists.

---

**Dev A — Alert Rules:**
- Set up alert rules in Prometheus/Grafana:
  - P99 latency for `POST /trips/{id}/plan` exceeds 30s → alert
  - Error rate for any endpoint exceeds 5% over 5 minutes → alert
  - `mcp_cache_hit_ratio` drops below 0.3 → alert (something wrong with Redis or cache warming)
  - `llm_calls_total{status="error"}` rate exceeds 10/min → alert
- Test each alert by deliberately triggering the condition
- **Acceptance:** All 4 alerts fire correctly under deliberate test conditions. Alert messages include enough context to diagnose (which endpoint, which metric, current vs. threshold).

**Dev B — Runbook, Dashboards & Deep Health Check:**
- `docs/runbook.md`: for each alert, a step-by-step diagnosis guide — what to check first, which logs/traces to look at, common causes, known fixes
- `GET /admin/health-deep`: runs `SELECT 1` against Postgres, pings Redis, calls `estimate_budget` to verify MCP server alive, checks LangSmith connectivity — returns JSON health object per dependency
- Grafana dashboard polish: RED metrics + LLM cost + cache hit ratio + embedding queue depth — all on one dashboard
- **Acceptance:** Runbook covers all 4 alert conditions. `GET /admin/health-deep` returns correct status for all dependencies. Grafana dashboard shows real data without errors.

---

### Phase 35 — Security Hardening

**What this phase delivers:** OWASP Top-10 relevant items reviewed and addressed. API keys never logged.

---

**Dev A — Auth & Input Hardening:**
- Add refresh token flow: `POST /auth/refresh` so JWTs can be short-lived (1h) without forcing re-login
- Rate limiting on auth endpoints: `POST /auth/login` → max 5 attempts per IP per minute
- Input validation review: all Pydantic schemas get `max_length` constraints on string fields; no injection via `destination` or `interests`
- `DECISIONS.md` entry: which OWASP Top-10 items are relevant and how each is addressed (or accepted as known limitation)
- **Acceptance:** Refresh token flow works. Login rate limiting fires at 5 attempts. All string fields have `max_length`. OWASP review documented.

**Dev B — Secret Management & Logging Audit:**
- Grep all log output for known key patterns (Amadeus client IDs, Google Maps `AIza...` prefix, OWM 32-char hex keys). If any found: `LogRedactor` logging filter scrubs them before output
- Rotate all dev API keys and confirm the system still works (proves no hardcoded keys anywhere)
- Add `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options` headers to all API responses via FastAPI middleware
- **Acceptance:** Zero API key patterns in log output. Key rotation test passes. Security headers present on all responses (verify with `curl -I`).

---

### Phase 36 — Multi-Language Support Foundation

**What this phase delivers:** A Hindi query produces a correctly structured itinerary. Language preference stored in user preferences.

---

**Dev A — Input Language Handling:**
- `LanguageDetectionNode` at the start of Orchestrator: detects input language via `langdetect`; if not English → translate to English via Gemini Flash before intent parsing; translate final itinerary back to detected language
- Language preference stored in `user_preferences.preferred_language` — if set, skip detection
- Supported languages for this phase: Hindi, Tamil, Bengali — document which were tested and which failed in `DECISIONS.md`
- **Acceptance:** "गोवा में 5 दिन की यात्रा, बजट ₹50,000" → correctly parsed, itinerary produced in Hindi. English-input users see zero change. Language detection correct for 3 test inputs per language.

**Dev B — Itinerary Language Localisation:**
- ItineraryBuilder prompt updated: when `preferred_language != "en"`, produce output in the target language (activity descriptions in local script where available)
- `user_preferences.preferred_language` persists — all subsequent trips returned in that language
- **Acceptance:** Hindi-language itinerary stored in `structured_data` with Hindi text in descriptions. `preferred_language` persists across sessions.

---

### Phase 37 — Advanced Itinerary Features

**What this phase delivers:** Time-of-day scheduling, travel time between activities, realistic day planning.

---

**Dev A — Time-of-Day Scheduling:**
- Extend `ItineraryBuilder` to assign realistic time slots: morning = 08:00–12:00, afternoon = 13:00–17:00, evening = 18:00–22:00
- If two activities are > 10km apart (calculate from `lat`/`lng`), insert a "travel" slot with estimated time and mode (auto-rickshaw, taxi, walk)
- Update `structured_data` to include `start_time`, `end_time`, `travel_from_previous_minutes` per activity slot
- **Acceptance:** Each activity slot has `start_time` and `end_time`. Activities > 10km apart have a travel note. Day does not exceed 14 hours of activity. 3 itinerary structure tests pass.

**Dev B — Travel Time MCP Tool:**
- Add `get_travel_time(origin_lat, origin_lng, dest_lat, dest_lng, mode)` as a new MCP tool backed by the Google Maps Distance Matrix API
- Cache results for 24h (travel times between static locations don't change)
- Called by ItineraryBuilder when inserting travel estimates
- **Acceptance:** Tool returns correct travel time for a known route (e.g. Fort Aguada to Baga Beach). Cached on second call (`[CACHE HIT]` in logs).

---

### Phase 38 — Weather-Aware Itinerary

**What this phase delivers:** Rain forecast on day 3 → outdoor activities moved to covered venues or indoor alternatives.

---

**Dev A — Weather-Aware Builder Logic:**
- Extend ItineraryBuilder prompt: if `get_weather` returns "Rainy" for a day → prefer indoor activities (museums, restaurants) over outdoor ones (beaches, forts) for that day
- If no indoor alternatives available in the attractions list → note in the itinerary that weather may be poor and outdoor plans should be flexible
- Weather context passed to builder as a day-by-day array alongside attractions data
- **Acceptance:** Day with "Rainy" forecast → at least 1 indoor activity in that day's slots. Day with "Sunny" → beach/outdoor activities not excluded. Tested with a known rainy-day forecast fixture.

**Dev B — Real Weather Display in Frontend:**
- Weather icons on each day card: sunny, partly cloudy, rainy (from the `condition` field in `DayForecast`)
- "(climate estimate)" badge on days beyond OWM's 5-day window so users know it's an estimate
- **Acceptance:** Weather icon renders correctly for all 3 condition types. Climate estimate badge visible on day 6+.

---

### Phase 39 — Booking Deep Links

**What this phase delivers:** Each flight and hotel has a "Book Now" link pointing to the correct pre-filled booking page.

---

**Dev A — Deep Link Generation:**
- Flights: generate Google Flights deep link `https://www.google.com/flights?q=DEL+GOI+2025-12-10` with actual parameters from the search result
- Hotels: generate Booking.com or MakeMyTrip deep link with destination, check-in, check-out pre-filled
- Store links in `structured_data` alongside flight/hotel data
- **Acceptance:** Each flight result has a Google Flights deep link pre-filling correct route and date. Each hotel result has a Booking.com deep link. Links open to correct search results in a browser.

**Dev B — Booking Links in Frontend:**
- "Book Flight" and "Book Hotel" buttons on the relevant cards; opens in new tab (`target="_blank"`, `rel="noopener noreferrer"`)
- Button disabled (greyed out) if link is null
- **Acceptance:** Buttons present and functional. New tab opens. Disabled state correct for null links.

---

### Phase 40 — Notification System

**What this phase delivers:** User receives an email when their itinerary is ready. Trip reminder 24h before departure.

---

**Dev A — Email Notification Service:**
- Email via `sendgrid` (or `boto3` SES) — log-based stub in dev, real sending in production (gated by `SENDGRID_API_KEY`)
- Two triggers: (1) `planning_complete` → "Your itinerary is ready" email with summary and link. (2) `start_date - 24h` → "Your trip starts tomorrow" reminder with day 1 itinerary
- Background task — never blocks the HTTP response
- **Acceptance:** `planning_complete` → email logged (dev) / sent (production). Reminder fires at correct time (tested with injectable `now()`, same pattern as Phase 3's expiry test). No email delays the HTTP response.

**Dev B — Notification Preferences & Frontend:**
- `user_preferences.notification_email: bool` (default true); `user_preferences.reminder_hours_before: int` (default 24)
- Frontend settings page: toggle email notifications on/off, set reminder timing
- **Acceptance:** Notifications respect `notification_email = false`. Reminder timing configurable and respected. Settings page renders and saves correctly.

---

### Phase 41 — Admin Dashboard

**What this phase delivers:** Admin can see all trips, costs, and errors. Not exposed to regular users.

---

**Dev A — Admin Endpoints:**
- `GET /admin/trips`: all trips across all users, filterable by status and date range. Requires `role = "platform_admin"` JWT claim
- `GET /admin/costs`: LLM costs by date and agent (from Phase 26's `agent_runs.cost_usd`)
- `GET /admin/errors`: recent `agent_runs` rows with `status = "failed"`, joinable to `trips`
- Admin-specific JWT: add `role` claim to JWT payload; set `platform_admin` via `scripts/seed_admin.py`
- **Acceptance:** Regular user JWT → 403 on all admin endpoints. Admin JWT → 200. Correct filtering. Seed admin script works.

**Dev B — Admin Frontend:**
- `/admin` route (Next.js, protected by admin JWT check): tables for active trips (status, destination, created_at), recent errors (trip_id, agent_name, error reason), LLM cost by day (bar chart via Recharts)
- **Acceptance:** `/admin` renders for admin user, redirects to login for regular users. All three sections show real data.

---

### Phase 42 — Advanced pgvector: Semantic Trip Search

**What this phase delivers:** Free-text semantic search returns relevant past trips. "Beach trip under ₹50k" finds beach itineraries even without those exact words.

---

**Dev A — Semantic Search Enhancement:**
- Extend `GET /trips/search?q=...` with hybrid search: combine pgvector cosine similarity with a keyword match on `destination` field — document which gives better results in `DECISIONS.md`
- Add filters: `?budget_max=50000`, `?destination=Goa`, `?days_min=5` — applied as post-filter on pgvector results
- **Acceptance:** "Beach trip under ₹50k" → returns beach itineraries, not mountain treks. Filters work correctly. Hybrid search strategy documented.

**Dev B — Search UI:**
- Search bar on trips list page: real-time search with 300ms debounce, calls `GET /trips/search?q=...`
- Results: destination, dates, total cost, top 3 activities as chips
- Empty state: "No similar trips found — be the first!" with link to plan a new trip
- **Acceptance:** Search bar functional with debouncing. Results render correctly. Empty state renders.

---

### Phase 43 — Webhook Notifications

**What this phase delivers:** Third-party integrations can subscribe to trip planning events via webhooks.

---

**Dev A — Webhook Delivery System:**
- `POST /webhooks/subscribe`: accepts `{url, secret, events: ["planning_complete", "planning_failed", "budget_conflict"]}` — stored in `webhook_subscriptions` table
- On each event: look up subscriptions, deliver signed payload via `httpx.post()` with `X-Signature: sha256=<hmac>` header
- Retry with `tenacity`: 4 attempts, exponential backoff with jitter, DLQ on exhaustion
- **Acceptance:** Subscribe → receive `planning_complete` webhook on registered URL. Signature verifies. DLQ fires after 4 failures.

**Dev B — Webhook Management Frontend:**
- Settings page "Webhooks" section: list registered webhooks, add new, delete existing
- Webhook delivery log via `GET /webhooks/deliveries`: recent delivery attempts with status (delivered, failed, retrying, dlq)
- **Acceptance:** Webhooks CRUD works end to end. Delivery log shows correct statuses.

---

### Phase 44 — Final System Hardening

**What this phase delivers:** All known bugs documented or fixed. System deploys from a fresh clone with only `.env` filled in.

---

**Dev A — Bug Sweep & Known Limitations:**
- Run full eval suite from Phase 33 — fix any score drops since last run
- Review all `TODO` and `# FIXME` comments in the codebase — resolve or document each as a known limitation in `DECISIONS.md`
- Confirm `alembic upgrade head` works on a fresh Postgres instance (no dependency on existing data)
- **Acceptance:** Eval suite scores within 5% of Phase 33 baseline. All `TODO` comments resolved or documented. Fresh Postgres + `alembic upgrade head` → clean schema.

**Dev B — Fresh Clone Deploy Test:**
- Clone the repo into a completely fresh directory, follow only the README instructions with only `.env` filled in, confirm the full system runs
- Any step requiring knowledge not in the README is a bug — fix the README or add the missing script
- **Acceptance:** Fresh clone → Docker up → migrations → backend running → frontend running → `curl /ping` → "ok". Zero undocumented steps.

---

## Chapter 6 — Documentation & Demo (Phases 45–50)

### Phase 45 — Architecture Documentation

**What this phase delivers:** Accurate architecture diagram and system documentation reflecting what was actually built.

---

**Dev A — Architecture Diagram:**
- Finalize the architecture diagram to match what was actually built (note deviations from the original plan and why)
- Cover: every LangGraph node (with agent name and LLM used), every MCP tool (with external API it calls), Redis pub/sub channels, SSE connection flow, pgvector similarity search flow, Alembic migration chain
- Use `mermaid` syntax in `docs/architecture.md` so it renders in GitHub
- **Acceptance:** Diagram matches running code (verified by code review). All agents, MCP tools, SSE flow, and pgvector visible.

**Dev B — API Documentation:**
- Review Swagger UI (`GET /docs`) — all endpoints have descriptions, all schemas documented with examples
- `docs/api-guide.md`: human-readable guide to key flows (plan a trip, refine a trip, search similar trips, export PDF) with example curl commands
- Document all SSE event types: `connected`, `agent_update`, `budget_conflict`, `budget_alternatives`, `builder_token`, `planning_complete`, `planning_failed` — with example payloads
- **Acceptance:** Swagger UI complete. `api-guide.md` covers all 5 key flows with working curl commands. SSE event format documented with examples.

---

### Phase 46 — Developer Experience

**What this phase delivers:** A new developer can run the full system locally within 30 minutes following only the README.

---

**Dev A — Local Dev Tooling:**
- `make` targets: `make up`, `make migrate`, `make test`, `make test-integration`, `make eval`, `make seed`
- `scripts/seed_data.py`: creates a test user, runs 3 sample trip plannings (Goa, Jaipur, Kerala), stores results — system has real data to explore immediately after setup
- **Acceptance:** `make up && make migrate && make seed` → system has 3 real itineraries. All make targets work on a clean checkout. `make test` runs in under 2 minutes.

**Dev B — Environment Setup Documentation:**
- `docs/local-setup.md`: step-by-step from "install Docker" to "see itinerary in browser" — exact commands, not vague instructions
- Document which API keys are optional vs. required for which features: without Amadeus → flights return `ToolError` (system still functional); without Google Maps → attractions degrade; without OWM → climate estimates used
- `docker-compose.override.yml` example showing how to point a local MCP server at a different port without modifying the main compose file
- **Acceptance:** A teammate who hasn't worked on the project follows `docs/local-setup.md` and has the system running in under 30 minutes (timed).

---

### Phase 47 — Retrospective & Lessons Learned

**What this phase delivers:** Honest `docs/retrospective.md` documenting real lessons, including what broke.

---

**Dev A — Technical Retrospective:**
- `docs/retrospective.md` sections:
  - Which phases took longer than planned and why (be specific — "Phase 9's intent parsing took 2 extra days because Gemini Flash consistently hallucinated the budget field when the user said 'around ₹50k' vs '₹50,000'")
  - Hardest prompt engineering challenges encountered, with before/after examples of failing vs. working prompts
  - At least 3 real bugs found during chaos testing (Phases 31–32) with the exact mechanism
  - What you would not do again: specific technical decisions that caused pain
- **Acceptance:** Retrospective is specific and honest — no vague "we learned a lot about LangGraph." Each point references a specific phase or incident.

**Dev B — Eval Score History & What's Left:**
- Add to the retrospective: eval score over time (from `eval-history.csv`) — where did scores drop and why?
- "What the system still doesn't handle well" — at least 5 honest items with severity rating (cosmetic / functional / architectural)
- "What we'd add given another month" — 3 specific features with rough effort estimates
- **Acceptance:** Score history plotted or tabulated. 5+ honest known limitations. 3 future features with estimates.

---

### Phase 48 — Interview Preparation

**What this phase delivers:** Both developers can answer every question on the Interview-Readiness Checklist out loud, without notes.

---

**Dev A — Agent Architecture Questions:**
- Practice explaining: (1) Why LangGraph over a custom agent loop? (2) Why Gemini Flash for orchestration and Claude Haiku for building? (3) The complete re-planning flow on a whiteboard from memory. (4) What happens when all 3 agents fail — what does the user see? (5) How does the Evaluator's retry cap prevent infinite loops?
- **Acceptance:** Both devs can answer all 5 questions correctly and concisely (under 3 minutes each) without notes.

**Dev B — Infrastructure & Data Questions:**
- Practice explaining: (1) Why pgvector over a dedicated vector DB? (2) HNSW vs. IVFFlat trade-off and which this system uses. (3) How SSE and Redis pub/sub work together — draw the connection lifecycle. (4) What the embedding strategy is and why structured summary outperforms full text for similarity. (5) The load test bottleneck story: what was found, what was fixed, what the numbers were
- **Acceptance:** Both devs can answer all 5 questions correctly and concisely without notes.

---

### Phase 49 — Demo Video

**What this phase delivers:** A 5–10 minute screen recording showing the full system working end to end.

---

**Dev A — Happy Path & Failure Demo:**
- Record a screen capture covering:
  1. Type a trip request → watch agent progress panel update in real time via SSE
  2. Itinerary appears → map view with pins → PDF download
  3. Refine: "change hotels to beachfront" → only hotel section updates
  4. Budget conflict: force a low budget → budget conflict options appear via SSE
  5. Token-by-token streaming visible as ItineraryBuilder runs
- Audio narration explaining what's happening at each step
- **Acceptance:** Video is 5–10 minutes and covers all 5 points.

**Dev B — Observability Demo:**
- Continue the recording:
  6. Grafana dashboard with live traffic during the demo
  7. LangSmith trace for the just-completed planning run — zoom into the budget decision node span
  8. A forced agent failure → graceful degradation visible in the UI
  9. Semantic search: type "beach trip under ₹50k" → results appear
  10. Admin dashboard showing costs and recent errors
- **Acceptance:** Observability section covers all 5 points. Grafana and LangSmith shown with real data.

---

### Phase 50 — Final System Review & Handover

**What this phase delivers:** README complete. System deploys from a fresh clone. Every checklist item answered. Gaps documented honestly.

---

**Dev A & Dev B (Shared):**
- Architecture diagram confirmed still accurate after all Phase 46–49 changes
- README final pass: what the system does, architecture diagram embed, agent roles and responsibilities, how to run locally (link to `docs/local-setup.md`), how to run the eval suite, how to interpret LangSmith traces, link to the demo video
- Final eval suite run: all 15 scenarios. Any score drops → honest note in README under "known limitations" — not papered over
- Both developers independently answer every item in the Interview-Readiness Checklist below, out loud, without notes
- Final fresh-clone deploy test repeated (from Phase 44) — any regressions fixed
- **Acceptance:** README renders correctly on GitHub with architecture diagram. Fresh clone deploy works. Eval suite passes with average score ≥ 3.5. Both devs complete the checklist independently.

---

## Interview-Readiness Checklist

- [ ] Why the agent boundaries were drawn where they were (why 4 agents, not 1 or 10), and what you'd merge or split differently next time
- [ ] Why LangGraph was chosen over a custom agent loop or a simpler sequential chain
- [ ] Why Gemini Flash for orchestration and Claude Haiku for the itinerary builder — is this just cost, or is there a capability reason?
- [ ] The complete flow including all re-plan branches, from memory, on a whiteboard
- [ ] The Evaluator's exact failure types and how the retry cap works — what happens on the 4th failure?
- [ ] The budget conflict decision node's exact logic — when does it replan vs. escalate, and what are the thresholds?
- [ ] How Redis pub/sub and SSE are connected — draw the connection lifecycle from HTTP request to browser tab
- [ ] Why pgvector over a dedicated vector DB (Pinecone/Weaviate) — trade-offs, not just "it was simpler"
- [ ] The HNSW vs. IVFFlat distinction and which index this system uses and why
- [ ] The embedding strategy: why two embeddings per itinerary, and which one gives better similarity results
- [ ] At least 3 real bugs found during chaos testing — including the exact mechanism that caused each
- [ ] The actual load test numbers: baseline, bottleneck found, specific fix applied, after numbers
- [ ] How the MCP tool calls are cached, and why TTLs differ between flights (5 min) and attractions (6 hr)
- [ ] The complete conversation history and refinement flow — what state is preserved across turns, what is discarded
- [ ] What the system still doesn't handle well, and what you'd add given another month

**Honest note:** if any phase's task gets rushed or skipped, write that down plainly in the README rather than pretending it was done. A reviewer probing a weak spot you're upfront about lands better than one probing a weak spot you claimed was solid.


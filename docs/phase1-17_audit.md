# Phases 1–17 — End-to-End Audit (2026-10-02)

**Question:** is everything from Phase 1 to Phase 17 complete and working end to end?

**Answer before the audit:** no. The unit suite was green (246 tests) but the
system could not plan a trip when run for real. Every layer below had been
tested only through a mock of the layer where its bug lived.

**Answer now:** the full flow runs — real Postgres, real Redis, the real
LangGraph, real SSE, a real browser — with the external APIs stubbed. What is
still **not** verified is listed at the bottom, and it matters.

---

## How it was checked

| Check | Result |
|---|---|
| `ruff check .` and `ruff format --check .` | clean (was 3 lint errors, 27 unformatted files) |
| `pytest tests/unit tests/contract` | 288 passed |
| `RUN_INTEGRATION=1 pytest tests/integration` | 14 passed (was 5 of 9) |
| `alembic check` (models vs migrations) | no drift (was 13 differences) |
| `npx tsc --noEmit`, `next build` | clean |
| `npx playwright test` (Chromium) | 3 passed (the spec could not even load before) |
| `docker compose up backend` → `GET /ping` | `{"postgres":"ok","redis":"ok"}` (container could not start before) |
| Real `uvicorn` + real MCP subprocess, curl-driven | auth, CORS, clarification, SSE order, status, itinerary, runs, timeline all correct |

---

## What was broken

Severity: **A** = no trip could complete · **B** = a documented feature did not work · **C** = edge case / hygiene.

### A — the pipeline could not run

| # | Finding | Fix |
|---|---|---|
| A1 | **MCP client truncated every list result to its first element.** FastMCP sends one text block per list item; `call_tool` read `content[0]`. Flights/hotels/attractions arrived as a single dict and the budget node crashed iterating its keys. | Read `structuredContent`, fall back to all blocks (`mcp_client/client.py`). |
| A2 | **Concurrent agents shared one DB session.** HotelAgent and ActivitiesAgent run in `asyncio.gather` and both committed `agent_runs` rows on the same `AsyncSession` → `IllegalStateChangeError` / duplicate-key on every real run. | Per-session lock in `log_agent_run`. |
| A3 | **Flight/hotel provider no longer exists.** Amadeus self-service was shut down on 2026-07-17. Every flight search failed, and every failure was reported as a *budget conflict*. | Flights → Duffel. Provider outage → continue without that data (DECISIONS #47, #48). |
| A4 | **Flight search was given city names.** The orchestrator passed "Goa"; the tool sent it as an IATA code. | Tool resolves city → IATA. |
| A5 | **Evaluator compared itinerary cost to the user's budget.** Anything not within 5% of the budget was a `budget_mismatch` → 3 retries → failed. | Compare with a total recomputed from source prices (`expected_total_cost`). |
| A6 | **Retired model ID** `gemini-1.5-flash`, hard-coded in five places. | `GEMINI_MODEL` / `GROQ_MODEL` settings. |
| A7 | **API keys in `.env` never reached the LLM SDKs** (they read `os.environ`; pydantic-settings does not populate it). | `config.py` exports the three SDK keys. |
| A8 | **The documented run command failed**: `cd src/backend && uvicorn app.main:app` → `ModuleNotFoundError: No module named 'src'`. `.env` and the MCP subprocess also depended on the working directory. | Path bootstrap, anchored `.env`, MCP `cwd`. |
| A9 | **Docker backend could not start**: image lacked `src/ai`, the volume mount hid `alembic.ini`, `.env` pointed at `localhost`. | New Dockerfile, compose service, `.dockerignore`. |
| A10 | **Integration tests wiped the dev database** (`drop_all`) and left `alembic_version` at head, so `alembic upgrade head` then did nothing. The dev DB had zero tables. | Dedicated `<db>_test`, built with Alembic. |

### B — features that did not do what the phase says

| Phase | Finding | Fix |
|---|---|---|
| 5 | A JWT whose `sub` is not a UUID returned 500. The SSE route pinned a pooled DB connection for the life of the stream. | 401; connection released before streaming. |
| 7 | `POST /plan` never returned `clarification_needed` (docs said it did). | Deterministic check: no interests and no free text → question (DECISIONS #56). |
| 9 | A failed flight search published nothing on SSE; missing interests silently produced zero attractions. | Failures published; `["sightseeing"]` default. |
| 10 | Re-plan loop was a no-op (the tool ignored `budget`); `/replan` choices were not saved, so "increase budget" twice gave the same budget; `reduce_days` could produce end ≤ start. | Budget is a real cap + one more stop per attempt; choice persisted; 422 for too-short trips. |
| 11 | When retries ran out because of evaluator failures the reason shown was a generic builder message. | Failure reason carries the evaluator's details. |
| 12 | Builder prompt had no rule for an empty hotel/flight list. | Prompt v5. |
| 13 | A crashed run left the trip in `planning` forever. | Crash → `failed` + `planning_failed`; startup sweep. |
| 14 | `planning_complete` waited for the OpenAI call (≈7 s of back-off with no key). `embeddings_pending` counter and `GET /admin/embedding-health` were missing. Stale `pending_retry` rows were never deleted (`db.delete` not awaited). | Background task; counter + endpoint; `await`. |
| 15 | First refinement was labelled turn **1**. Refinements published no SSE events, did not save state (turn 3 refined turn 1's data), carried retry counters across turns, and rebuilt an identical itinerary because the message influenced nothing. "I'd rather go to Mumbai" kept the old destination. `refine()` had no tests. | See DECISIONS #55; 8 new tests + integration coverage. |
| 16 | — (worked as specified; now covered end to end). | |
| 17 | Timestamps had no UTC marker (browsers read them as local time). No error envelope. CORS was `*`+credentials in dev and empty elsewhere, with no documented production policy. `/status` showed the previous run's 3/3 during a re-plan. | DECISIONS #59, #60; status scoped to the run in flight. |
| 17 (frontend) | Trip page never loaded an existing trip or itinerary (reload → "Ready to plan"); failures showed "unknown error" (read the wrong field); a missed SSE event hung the UI; budget conflicts had no action; form labels were not bound to inputs. Flight cost pill was always ₹0. | Page rewritten around one load path and one `finishRun`; `GET /trips/{id}` added; replan buttons. |
| 17 (E2E) | The Playwright spec could not load (`@playwright/test` not resolvable from `tests/e2e`), used `getByLabel` on unbound labels, a non-strict `getByText("Flights")`, and an email domain the API rejects. It had never run. | Spec moved to `src/frontend/e2e/`, selectors fixed, stub backend auto-started. |

### C — smaller things

- Provider error messages contained the request URL **including the API key**, stored in `agent_runs` and sent over SSE. Now status code only.
- Malformed dates raised inside the flight/hotel tools instead of returning a `ToolError`.
- Trips could be created in the past; `group_size` had no upper bound (flight search allows 9).
- Double `POST /plan` started two orchestrators. Now 409.
- Intent parsing trusted the LLM's types; an LLM failure crashed the run. Now validated and non-fatal.
- Models and migrations disagreed (cascades, two indexes, a column type). `alembic check` is now clean and tested.
- Deprecated `setex`, un-awaited coroutines in tests, duplicate/broken root `playwright.config.ts`.

---

## What the old tests could not see

| Mocked seam | Bug hidden behind it |
|---|---|
| `call_tool` | A1 — list truncation in the real MCP client |
| `AsyncSession` | A2 — concurrent commits; un-awaited `db.delete` |
| `EvaluatorAgent` in orchestrator tests | A5 — budget comparison |
| `OrchestratorAgent` in route tests | crashed runs, missing state save, turn numbering |
| Amadeus SDK | A3, A4 — dead provider, city names as IATA codes |

`tests/integration/test_pipeline_integration.py` now drives plan → refine → add-day, budget conflict → replan, and a no-provider run through the HTTP API with only the network stubbed. It is the test that found A2.

---

## Still not verified — read this before a demo

1. **No live LLM or provider call has succeeded.** The keys in the local `.env` are placeholders. Duffel and OpenTripMap were reached and answered 401, so URLs and headers are plausible, but a real Duffel offer response, Gemini `gemini-3.5-flash`, and Groq have not been exercised. The Duffel mapping is unit-tested against the documented response shape only.
2. **Hotels have no provider.** `search_hotels` returns `API_NOT_CONFIGURED`; itineraries are built without a hotel. Choosing one (Duffel Stays, Booking, …) is an open decision.
3. **Currency conversion is a static table** (INR/USD/EUR/GBP). A Duffel account billed in another currency returns `NO_RESULTS`.
4. **Prompts v5 (builder) and the orchestrator's `sightseeing` rule have not been run against a live model.**
5. **`add_day` always adds exactly one day**, whatever the message says.

## Not done on purpose

- shadcn/ui is listed in the roadmap; the frontend uses plain Tailwind components. Left as is.
- The roadmap asks for the LangGraph `Send` API; `asyncio.gather` is used (DECISIONS #15).
- The evaluator is deterministic rather than Claude Haiku (DECISIONS #21); the builder is Groq Llama 3.3 rather than Claude Haiku (free tier).

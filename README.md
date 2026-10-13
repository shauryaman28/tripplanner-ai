# AI Trip Planner

> Multi-agent AI travel planner — flights, hotels, activities & itineraries.
> Built with FastAPI · LangGraph · MCP · Gemini Flash · Groq gpt-oss-120b · pgvector.

**Status: Phase 31 / 50 — Chaos Engineering: Agent Failures**

---

## Architecture

```
User → Next.js 14 → FastAPI Gateway → OrchestratorAgent (LangGraph)
                         │                      │
                    JWT auth              ┌─────┼─────────────┐
                    SSE stream            ▼     ▼             ▼
                    Redis pub/sub   FlightAgent HotelAgent ActivitiesAgent   DestinationIntelligenceAgent
                                          │     │             │              (local tips, from what the
                                          └─────┴─────────────┘               model knows — no tools)
                                                │
                                        MCP Server (5 tools, run side by side)
                              Redis cache → rate limit → backoff on 429 / 5xx
                         Duffel · LiteAPI · OpenTripMap · OpenWeatherMap
                                                │
                                   ItineraryBuilder (Groq gpt-oss-120b)
                                      → streamed to the page as it is written (builder_token)
                                      → Evaluator (deterministic)
                                                │
                                      Postgres + pgvector
                                         Redis pub/sub
                                        SSE → Frontend

Every saved itinerary is embedded (Gemini, 1536 dimensions, pgvector). GET /trips/{id}/similar ranks a
traveller's other trips by cosine similarity to this one; GET /trips/search?q=… ranks them against a
typed query. Both search only the caller's own trips.

Every run can be a LangSmith trace (off unless .env switches it on): a span for each step, model call
and tool call, tagged with the trip and the user. Every step that asked a model records its tokens and
their list price in agent_runs; GET /admin/cost-summary adds them up by agent and by day.

Deployed (Phase 27): one Docker image configured by its environment, on Railway; Postgres + pgvector on
Neon, through its connection pooler; Redis on Upstash over TCP with TLS (pub/sub needs a connection — its
REST API cannot subscribe); the frontend on Vercel, whose proxy carries /api/* while the browser opens the
event stream straight to the backend; errors to Sentry, scrubbed. `python -m app.preflight` says what a
deployment is missing before it starts; a deploy does not fail the plans being made.

Checked on every push (Phase 28): GitHub Actions runs two jobs side by side — the backend's (ruff, every
migration up / compared with the models / down / up on a real Postgres, the unit and contract tests) and the
frontend's (types, lint, a production build). On main, a third job follows only when both pass: it deploys
the backend to Railway and waits until the public addresses answer GET /version with the merged commit.
Every night at 02:00 UTC the integration tests run on a real Postgres and Redis, and the result is one
comment on the repository's "Nightly test results" issue.

Seen from outside (Phase 29): GET /metrics for Prometheus — every request counted and timed per endpoint,
and the app's own numbers: model calls by agent and model, the tool server's cache hits, itineraries built,
how long a plan takes. One OpenTelemetry trace per request, from the request through every SQL statement
and Redis command, into the tool server's process and back, to the row that is written. Every log line a
JSON object carrying trip_id, user_id and its trace. One trip id finds all three; a Grafana dashboard
shows them.

Under load (Phase 30): a planning run holds no database connection while it waits for a model or a provider —
every read's transaction is ended first — so the connections in use follow the statements being run, not the
plans in flight. The database pool and the Redis pool are settings (DB_POOL_SIZE, DB_MAX_OVERFLOW,
REDIS_MAX_CONNECTIONS), and GET /metrics says how full each is and how late the event loop runs. Measured on
one laptop with stand-ins for the models and providers: 3.2 visits a second, each one a plan, on one process.

When something it depends on stops (Phase 31): a tool server that dies is replaced by the next search, which is
asked again of the new one; a plan made without one of its searches says in the itinerary what it lacks and why;
the model that writes the itinerary is waited for 30 seconds and a planning run 5 minutes, never for ever; Redis
being away slows no run, and is answered 503 "try again shortly" where a request cannot begin without it.

A planned trip downloads as a PDF: GET /trips/{id}/export/pdf → ReportLab lays out the saved
itinerary (cover, day by day, local tips, cost breakdown) and adds a map drawn from OpenStreetMap tiles.
Text in the scripts of India is drawn in Noto and shaped by HarfBuzz.
```

---

## Quick Start

### Prerequisites
- Docker Desktop
- Python 3.11 (CI and Docker use it; 3.10 is the oldest that works — the models use `X | None`)
- Node.js 18+ (for Next.js frontend)

### 1. Clone & configure
```bash
git clone https://github.com/shauryaman28/tripplanner-ai.git
cd tripplanner-ai
cp .env.example .env
# Edit .env — set JWT_SECRET at minimum (generate with: openssl rand -hex 32)
```

### 2. Start infrastructure
```bash
docker compose up postgres redis -d
```

### 3. Install dependencies & run migrations
```bash
pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt   # the versions the tests run on
alembic upgrade head
```

### 4. Run the backend
```bash
cd src/backend
uvicorn app.main:app --reload --port 8000
```
`.env` is always read from the repo root, wherever you start the server from.
To also reload on changes under `src/ai`, run from the repo root instead:
`uvicorn app.main:app --reload --app-dir src/backend --reload-dir src`.

### 5. Run the frontend (Phase 17)
```bash
cd src/frontend
npm install
npm run dev
# Open http://localhost:3000 in your browser
```

### 6. Verify Phase 1 done criterion
```bash
curl http://localhost:8000/ping
# → {"postgres": "ok", "redis": "ok"}
```

### 7. Run tests
```bash
# Unit + contract tests (no Docker, no network) — 1,253 tests
pytest tests/unit/ tests/contract/ -v

# Integration tests (Docker Postgres + Redis) — 74 tests, incl. the full
# plan → export → refine → replan pipeline through the HTTP API. They use their own
# `tripplanner_db_test` database, so dev data is never touched.
RUN_INTEGRATION=1 pytest tests/integration/ -v

# Browser tests (Playwright) — 60 tests: 5 end-to-end flows, 4 for Phase 25's group trips, 8 for Phase 23's
# similar trips and search, 10 for Phase 22's local tips, 3 for Phase 21's priced
# budget conflict, 5 for Phase 20's polish (live draft, refinement marks, retry, 375 px), 1 for Phase 24's
# rate-limited search, 16 for the live draft's reader, 8 for the change summary. Starts its own stack: a stub backend
# (real app, DB, Redis and graph; external APIs faked) on :8100 with its own
# `tripplanner_db_e2e` database, and a second Next.js dev server on :3100.
cd src/frontend
npx playwright install chromium   # once
npx playwright test               # uses the repo's .venv by itself; PYTHON=/path/to/python picks another

# Frontend static checks
npx tsc --noEmit && npm run lint
```
The E2E stack uses its own ports, database and build directory, so it can run
while your dev servers (:8000 / :3000) are up and never touches dev data.

---

## Project Structure

```
tripplanner-ai/
├── src/
│   ├── frontend/                        ← Phases 17–23: Next.js 14 App Router
│   │   ├── package.json
│   │   ├── next.config.mjs              ← /api/* proxy → FastAPI backend; Phase 27: a deployment build stops
│   │   │                                  if the backend's address was not given; Phase 28: the build is told
│   │   │                                  its commit
│   │   ├── tailwind.config.ts           ← design tokens: neutrals, status colours, shadows, motion
│   │   ├── tsconfig.json
│   │   ├── playwright.config.ts         ← starts the E2E stack (stub backend :8100, dev server :3100)
│   │   ├── playwright.production.config.ts ← Phase 27: one check, in a browser, of a site that is already running
│   │   ├── e2e-production/smoke.spec.ts ← Phase 27: a traveller plans a trip on the deployed site (real everything)
│   │   ├── e2e/planning.spec.ts         ← Playwright tests: plan, map, refine, PDF download, budget conflict
│   │   ├── e2e/polish.spec.ts           ← Phase 20: live draft, refinement marks, retry, every view at 375 px
│   │   ├── e2e/budget.spec.ts           ← Phase 21: a budget conflict priced, and each way out planned
│   │   ├── e2e/tips.spec.ts             ← Phase 22: the Local tips accordion; no section when there are no tips
│   │   ├── e2e/similar.spec.ts          ← Phase 23: similar trips on the trip page; search on the trips list
│   │   ├── e2e/group.spec.ts            ← Phase 25: four travellers, a stop for each, and each one's share
│   │   ├── e2e/changes.spec.ts          ← what the assistant says changed (pure function tests)
│   │   ├── e2e/draft.spec.ts            ← the live draft's partial-JSON reader (pure function tests)
│   │   └── src/
│   │       ├── app/
│   │       │   ├── globals.css          ← component classes (.btn, .card, .pin…) and Leaflet overrides
│   │       │   ├── layout.tsx           ← fonts (Inter + Fraunces)
│   │       │   ├── page.tsx             ← redirects → /trips
│   │       │   ├── login/page.tsx       ← sign in / create account
│   │       │   ├── version/route.ts     ← Phase 28: GET /version — the commit this site was built from
│   │       │   └── trips/
│   │       │       ├── page.tsx         ← trip cards, new-trip form with "Who is going" (Phase 25), search (Phase 23)
│   │       │       └── [id]/page.tsx    ← the plan, the assistant, live progress
│   │       ├── components/              ← ItineraryView, CostSummary, DayCard, ItineraryMap (Phase 18),
│   │       │                              DownloadPdfButton + Toast (Phase 19), LiveDraft, ProgressSheet,
│   │       │                              TripStages (Phase 20), LocalTips (Phase 22), SimilarTrips (Phase 23),
│   │       │                              GroupPanel (Phase 25), AgentProgressPanel,
│   │       │                              MessageThread, ChatInput, AppHeader, Brand, ui
│   │       └── lib/                     ← api.ts, sse.ts (reconnecting EventSource), map.ts (pins, routes,
│   │                                      day colours), changes.ts (what a change request changed, and
│   │                                      which parts), draft.ts (the itinerary while it is written),
│   │                                      tips.ts (local tips), useWideScreen.ts, download.ts, places.ts,
│   │                                      format.ts, types.ts
│   ├── backend/
│   │   ├── Dockerfile                   ← Phase 27: the image that is deployed — listens on $PORT, pinned installs,
│   │   │                                  checks its settings, migrates, then becomes the server
│   │   └── app/
│   │       ├── main.py                  ← FastAPI entry point; Phase 27: startup that leaves another instance's
│   │       │                              runs alone, shutdown that waits for the plans in flight
│   │       ├── preflight.py             ← Phase 27: what a deployment is missing, said at once, before it starts
│   │       ├── api/
│   │       │   ├── deps.py              ← JWT dependencies
│   │       │   └── routes/
│   │       │       ├── auth.py          ← POST /auth/register, /auth/login
│   │       │       ├── health.py        ← GET /ping; GET /ready (Phase 27: the same, as a status a platform can read);
│   │       │       │                      GET /version (Phase 28: the commit that is running)
│   │       │       ├── trips.py         ← All trip routes + SSE + GET /status + POST /retry + GET /export/pdf
│   │       │       │                              + GET /similar and GET /search (Phase 23)
│   │       │       ├── users.py         ← GET/PUT /users/preferences (Phase 16)
│   │       │       └── admin.py         ← GET /admin/embedding-health (Phase 14), /admin/cost-summary (Phase 26),
│   │       │                              /admin/sentry-test (Phase 27: fails on purpose; needs OPS_TOKEN)
│   │       ├── core/
│   │       │   ├── config.py            ← pydantic-settings
│   │       │   ├── error_reporting.py   ← Phase 27: Sentry — on with a DSN — and what is scrubbed out of a report
│   │       │   └── security.py         ← JWT + password hashing
│   │       ├── db/
│   │       │   ├── session.py           ← async SQLAlchemy; Phase 30: the pool is a setting, and is reported
│   │       │   ├── url.py               ← Phase 27: a hosted Postgres's URL, as each driver needs it; nothing
│   │       │   │                          prepared is kept, so a pooler can sit in between
│   │       │   └── redis.py             ← async Redis singleton; Phase 27: retries a closed connection, pings an idle one
│   │       ├── models/                  ← SQLModel table models
│   │       ├── schemas/                 ← Pydantic request/response schemas
│   │       ├── search.py                ← Phase 23: similar trips and search — pgvector over itinerary summaries
│   │       └── pdf/                     ← Phase 19: the itinerary as a PDF (Phase 20: in the scripts of India)
│   │           ├── export.py            ← draw the map if it can be drawn, then build the PDF
│   │           ├── plan.py              ← what the pages say — pure, mirrors the trip page's rules
│   │           ├── static_map.py        ← map picture from tiles (httpx + Pillow), tiles cached in Redis
│   │           ├── document.py          ← the pages: cover, day by day, cost breakdown, map (ReportLab)
│   │           ├── flowables.py         ← pins, cards, chips, the logo — drawn on the PDF canvas
│   │           ├── scripts.py           ← a font per word, HarfBuzz shaping, right-to-left lines
│   │           ├── formatting.py, theme.py ← ₹ / dates / labels, and the site's colours and day palette
│   │           └── fonts/               ← Inter + Fraunces, and Noto for 13 scripts (noto/), all OFL,
│   │                                      with the script that builds them
│   ├── telemetry/                       ← Phase 29: what the running system says about itself — used by the API
│   │   │                                  server and by the tool server, importing neither
│   │   ├── context.py                   ← which trip, and whose, the work under way is for: two context variables
│   │   ├── metrics.py                   ← the app's own Prometheus metrics; every request counted per endpoint;
│   │   │                                  Phase 30: what runs out — the database pool, the Redis pool, the event loop
│   │   ├── traces.py                    ← OpenTelemetry: one trace from the request to the row; scrubbed on export
│   │   └── logs.py                      ← every log line a JSON object with trip_id, user_id and its trace
│   └── ai/
│       ├── llm.py                       ← model IDs + tolerant JSON parsing of LLM replies
│       ├── pricing.py                   ← Phase 21: seasons by destination, typical costs, the estimate arithmetic;
│       │                                  Phase 25: each traveller's share
│       ├── group.py                     ← Phase 25: who a place suits, the group score, taking places in turns,
│       │                                  repairing a plan so every two days hold a stop for each traveller
│       ├── tracing.py                   ← Phase 26: LangSmith on/off from the settings; a run tagged with its trip
│       │                                  and user; spans for a re-plan, an evaluator retry, a tool call
│       ├── usage.py                     ← Phase 26: what the models were asked (one recorder, the innermost timer),
│       │                                  the price table, tokens → dollars
│       ├── trace_tree.py                ← Phase 26: the runs LangSmith is sent, put back together as trees
│       ├── mcp_server/                  ← Phase 3: server, tools, models, cache; Phase 24: rate_limiter.py (each
│       │                                  provider's limit, a queue for the request over it), outbound.py (backoff
│       │                                  on 429 / 5xx), the tools in threads, one search per cache key at a time
│       ├── mcp_client/                  ← Phase 6: client.py talks to the MCP server; Phase 26: each call a span
│       ├── embeddings/                  ← Phase 14: Gemini embedding writer; Phase 23: the summary text, query embedding
│       ├── utils/
│       │   ├── run_logger.py            ← Phase 6: writes agent_runs; Phase 26: with each run's tokens and cost,
│       │   │                              and a warning when one trip's models pass LLM_COST_ALERT_USD; Phase 30:
│       │   │                              a row is written and its transaction is over — hand_back() for the places
│       │   │                              that read and then wait
│       │   ├── conversation.py          ← Phase 7B/15: Redis history + planning state
│       │   ├── preferences.py           ← Phase 16: preference loading / injection
│       │   ├── failures.py              ← Phase 20: a failed search in the traveller's words; retry or not
│       │   └── tasks.py                 ← fire-and-forget background tasks
│       ├── agents/
│       │   ├── flight_agent.py          ← Phase 6–7: 3-node graph (parse → route → search/clarify)
│       │   ├── hotel_agent.py           ← Phase 8 Dev A: 3-node graph, hotel-specific routing
│       │   ├── activities_agent.py      ← Phase 8 Dev B: 3-node graph, dual-requirement router
│       │   ├── budget_decision.py       ← Phase 10: pure budget threshold logic
│       │   ├── budget_alternatives.py   ← Phase 21: a budget conflict's ways out, priced without a search
│       │   ├── evaluator.py             ← Phase 11: 4 deterministic itinerary checks + retry routing;
│       │   │                                  Phase 21: the total within the estimate's range, hotels at the searched price
│       │   ├── refinement_classifier.py ← Phase 15: which agents a follow-up message re-runs
│       │   ├── destination_intelligence.py ← Phase 22: local tips from what a model knows — no tools
│       │   └── preference_extractor.py  ← Phase 16: learning lasting preferences from trips
│       ├── builder/
│       │   └── builder.py               ← Phase 12: ItineraryBuilder (Groq gpt-oss-120b), data-scope + budget-math validation;
│       │                                      Phase 20: streams its reply
│       └── orchestrator/
│           ├── orchestrator.py          ← Phase 9–22: full graph with preference injection, refinement & loops,
│           │                                  builder_token streaming, retry of one search, local tips;
│           │                                  Phase 26: every run a trace of its trip
│           └── warming.py               ← Phase 24: a trip's opening searches, made while it is being created
├── migrations/                          ← Alembic migrations
│   └── versions/
│       ├── 001_initial_schema.py        ← All 5 tables + pgvector
│       ├── 002_add_turn_to_agent_runs.py← Phase 15: turn tracking
│       ├── 003_add_user_preferences.py  ← Phase 16: user_preferences table
│       ├── 004_embedding_kind.py        ← Phase 23: embeddings.kind, HNSW index over summaries, re-embedding queued
│       ├── 005_trip_group_members.py    ← Phase 25: trips.group_members
│       └── 006_agent_run_tokens.py      ← Phase 26: agent_runs.token_count, agent_runs.cost_usd
├── tests/
│   ├── unit/                            ← Fast, no network, mock everything (1,086 tests)
│   ├── contract/                        ← Response shape tests (mocked, 8 tests)
│   ├── integration/                     ← Real Postgres + Redis (RUN_INTEGRATION=1, 74 tests)
│   ├── database.py                      ← separate test databases (<db>_test, <db>_e2e), migrated with Alembic
│   ├── e2e/stub_backend.py              ← the real app with external APIs stubbed, for Playwright
│   ├── ci/fake_gh.sh                    ← Phase 28: a stand-in for GitHub's CLI that writes down what it is asked
│   └── fakes.py                         ← network stubs shared by integration + E2E (APIs, LLMs, map tiles);
│                                          fake providers behind the real MCP tools (Phase 24), with an
│                                          attractions catalogue that answers by kind and popularity (Phase 25);
│                                          chat models that report token counts and a LangSmith that keeps
│                                          what it is sent and sends nothing anywhere (Phase 26)
├── .github/
│   ├── workflows/ci.yml                 ← Phase 28: every push — the backend's checks and the frontend's, side by
│   │                                      side; on main, the deploy after both
│   ├── workflows/deploy.yml             ← Phase 28: the backend to Railway, then a wait until the merged commit
│   │                                      is what answers; says "Nothing was deployed" until there is a target
│   ├── workflows/nightly.yml            ← Phase 28: 02:00 UTC — the integration tests, and one comment about them
│   └── nightly-issue.md                 ← what the nightly issue is opened with
├── docker/
│   ├── init.sql                         ← enables pgvector extension (local development only: migration 001 does
│   │                                      it for a database that was just created)
│   ├── prodlike/                        ← Phase 27: a production-like stack — the deployed image against a TLS-only
│   │                                      Postgres behind a pooler in transaction mode and a TLS-only Redis
│   ├── observability/                   ← Phase 29: Prometheus, Grafana, Jaeger, Loki and Alloy — what reads the
│   │                                      app's metrics, traces and log lines; the dashboard, as a file
│   ├── loadtest/                        ← Phase 30: the image the load test runs — the deployed one with the
│   │                                      stand-ins added — and the stack it is laid over
│   └── chaos/                           ← Phase 31: one more container on that stack — the far end of a model
│                                          call that goes wrong (it says 429, or 500, or never answers)
├── loadtest/                            ← Phase 30: what loads the app, and what stands in for the world outside it
│   ├── k6/traveller.js                  ← one traveller's visit, as the trip page makes its requests; visits start
│   │                                      at a fixed rate; a plan's duration is measured to its event on the stream
│   └── standins/                        ← what answers for the models and the five providers while a load test runs,
│                                          each taking as long as the real one does. Not in the image that is deployed,
│                                          and it refuses a database that is not on this machine. Phase 31: faults.py
│                                          (which model is to go wrong — then it is the provider's own client) and
│                                          fault_server.py (the server that client is pointed at)
├── scripts/                             ← run by hand: embedding_experiment.py (Phase 23's experiment, real model),
│                                          seed_demo_trips.py (a demo account with twelve embedded trips),
│                                          cache_ttls.py (Phase 24: every cached key's TTL against the spec),
│                                          group_experiment.py (Phase 25: four travellers against the real search),
│                                          trace_receiver.py (Phase 26: a local stand-in for LangSmith — prints
│                                          each trace it is sent; no account needed),
│                                          prodlike.py (Phase 27: the production-like stack up and down),
│                                          prod_check.py (Phase 27: asks a running deployment whether it is whole),
│                                          sentry_receiver.py (Phase 27: a local stand-in for Sentry),
│                                          freeze_constraints.py (Phase 27: writes constraints.txt again),
│                                          wait_for_release.py (Phase 28: waits until a deployment answers with
│                                          the commit that was merged), nightly_summary.py + nightly_post.sh
│                                          (Phase 28: the night's test reports as one comment, and its posting),
│                                          observability.py (Phase 29: the observability stack up and down),
│                                          observability_check.py (Phase 29: plans a trip, then asks Prometheus,
│                                          Jaeger, Loki and Grafana what they saw of it),
│                                          loadtest.py (Phase 30: the load test's stack, the load, and what the stack
│                                          did under it — every run kept as a file in docs/loadtest/),
│                                          chaos.py (Phase 31: the tool server killed, Gemini answering 429, the
│                                          builder's model silent, Redis killed — and what the app then does)
├── prompts/                             ← versioned LLM prompts (one file per version per agent)
├── docs/                                ← phase build logs (1–31) + phase1-17_audit.md + load-test-report.md
│                                          (Phase 30: before, the bottleneck, after) + loadtest/ (every run it is made of)
│                                          + chaos-report.md (Phase 31: expected, what happened, what was changed)
│                                          + chaos/ (every exercise's record, before and after)
├── DECISIONS.md                         ← architectural decision log
├── alembic.ini
├── docker-compose.yml
├── requirements.txt                     ← what the app needs, with lower bounds
├── constraints.txt                      ← Phase 27: the exact versions the suites run on — and the image installs
├── requirements-dev.txt
├── railway.json                         ← Phase 27: how Railway builds, health-checks and stops the backend
└── .env.example
```

---

## Phase Progress

| Phase | Description | Status | Tests |
|-------|-------------|--------|-------|
| 1 | Repo & Local Infrastructure | ✅ Done | 4 unit + 1 integration |
| 2 | MCP Server (protocol + mocked tools) | ✅ Done | 23 tool tests |
| 3 | MCP Server (real APIs: Duffel, LiteAPI, OpenTripMap, OWM) | ✅ Done | 36 tool + 7 contract tests |
| 4 | Database schema & migrations | ✅ Done | — |
| 5 | FastAPI gateway, JWT auth, SSE skeleton | ✅ Done | 5 auth + 6 trip + 4 health |
| 6 | FlightAgent: one agent, one tool | ✅ Done | 6 agent + 4 MCP client + 2 logger |
| 7 | Conditional edges: ask instead of assume | ✅ Done | 8 router + 5 clarification API |
| 8 | HotelAgent & ActivitiesAgent | ✅ Done | 6 hotel + 6 activities |
| 9 | Orchestrator: Decomposition & Fan-Out | ✅ Done | 5 orchestrator |
| 10 | Budget Conflict & Re-Planning | ✅ Done | 10 budget + 9 orchestrator |
| 11 | Evaluator Agent: Self-Checking | ✅ Done | 18 evaluator + 1 retry-chain |
| 12 | Itinerary Builder | ✅ Done | 6 builder + 8 orchestrator (new) + 2 integration |
| 13 | Persistence: Storing Every Run | ✅ Done | 9 agent_runs + timeline |
| 14 | Embedding Generation (Gemini, 1536-dim) | ✅ Done | 21 unit + 4 integration |
| 15 | Multi-Turn Refinement | ✅ Done | 27 unit tests |
| 16 | User Preferences & Personalisation | ✅ Done | 56 unit tests |
| 17 | Frontend: Chat Interface & SSE Streaming | ✅ Done | 6 status unit + 3 Playwright E2E |
| — | Change summary in the chat; refinements change only what was asked (DECISIONS #97–#99) | ✅ Done | 4 unit + 8 browser |
| — | Out-of-scope trip hardening: destination lookup, LLM fallback, day coverage (DECISIONS #90–#96) | ✅ Done | 18 unit + 1 integration |
| 1–17 | End-to-end audit ([docs/phase1-17_audit.md](docs/phase1-17_audit.md)) | ✅ Done | 26 regression unit + 5 pipeline/schema integration |
| 18 | Map View (Leaflet) + frontend redesign ([docs/phase18_build_log.md](docs/phase18_build_log.md)) | ✅ Done | 33 unit + 4 Playwright E2E |
| 19 | PDF Export — ReportLab, static map from OpenStreetMap tiles ([docs/phase19_build_log.md](docs/phase19_build_log.md)) | ✅ Done | 94 unit + 1 integration + 1 Playwright E2E |
| 20 | Frontend polish — streamed itinerary, refinement marks, retry, phone layout; the PDF in the scripts of India ([docs/phase20_build_log.md](docs/phase20_build_log.md)) | ✅ Done | 120 unit + 3 integration + 21 Playwright E2E |
| 21 | Smarter budget intelligence — seasons, a confidence range, a budget conflict priced three ways ([docs/phase21_build_log.md](docs/phase21_build_log.md)) | ✅ Done | 43 unit + 1 contract + 1 integration + 3 Playwright E2E |
| 22 | Destination Intelligence Agent — local tips from what a model knows, in an accordion and in the PDF ([docs/phase22_build_log.md](docs/phase22_build_log.md)) | ✅ Done | 51 unit + 2 integration + 10 Playwright E2E |
| 23 | pgvector similarity search — similar trips, search, the embedding experiment ([docs/phase23_build_log.md](docs/phase23_build_log.md)) | ✅ Done | 24 unit + 13 integration + 8 Playwright E2E |
| 24 | Caching & rate limits — cache warming on trip creation, a rate limit per provider, backoff on 429, the tools side by side ([docs/phase24_build_log.md](docs/phase24_build_log.md)) | ✅ Done | 90 unit + 3 integration + 1 Playwright E2E |
| 25 | Group trips — each traveller's interests searched on their own, a plan with a stop for everyone in every two days, each one's share of the cost ([docs/phase25_build_log.md](docs/phase25_build_log.md)) | ✅ Done | 69 unit + 4 integration + 4 Playwright E2E |
| 26 | LangSmith tracing & token costs — every run a trace filterable by trip, re-plans and evaluator retries as spans of their own, tokens and list price on every agent run, a cost summary and an over-cost alert ([docs/phase26_build_log.md](docs/phase26_build_log.md)) | ✅ Done — read back from LangSmith itself on 2026-10-11 | 46 unit + 8 integration |
| 27 | Production infrastructure — the image, Neon's URL and pooler, Upstash over TCP, Sentry with scrubbed reports, runs that survive a deploy, a pre-start check ([docs/phase27_build_log.md](docs/phase27_build_log.md)) | ✅ Built, and verified on a production-like stack — **not yet deployed**: that needs accounts on the five services | 80 unit + 9 integration + 1 browser check of a running deployment |
| 28 | CI/CD pipeline — two checks on every push, a deploy on main only after both, a wait until the merged commit is live, the integration tests every night with one comment about them ([docs/phase28_build_log.md](docs/phase28_build_log.md)) | ✅ Built, and every job run by a local runner on the workflow files as they are — **not yet seen on GitHub**, and the deploy has no target yet: see the build log | 30 unit |
| 29 | Observability — Prometheus metrics per endpoint and of the app's own (model calls, cache hits, builds, plan durations), one OpenTelemetry trace from the request through both processes to the row, JSON log lines searchable by trip, a Grafana dashboard ([docs/phase29_build_log.md](docs/phase29_build_log.md)) | ✅ Built, and verified on the production-like stack with Prometheus, Grafana, Jaeger and Loki beside it — **not on a deployment**: there is none yet | 84 unit + 4 integration |
| 30 | Load testing & performance baseline — k6 visits (create, stream, plan, itinerary) against the deployed image with stand-ins for the models and providers; before: 1.3 req/s at 210 ms p99, the bottleneck database connections held idle inside a transaction by every plan in flight; after: 43 req/s at 206 ms p99 ([docs/load-test-report.md](docs/load-test-report.md), [docs/phase30_build_log.md](docs/phase30_build_log.md)) | ✅ Measured on one laptop that ran both the load and the system — **not on a deployment**: there is none yet | 106 unit + 1 integration |
| 31 | Chaos engineering — the tool server killed mid-plan, Gemini answering 429, the builder's model never answering, Redis killed under open streams; each written down as a hypothesis first, run on the app as it was, fixed, and run again ([docs/chaos-report.md](docs/chaos-report.md), [docs/phase31_build_log.md](docs/phase31_build_log.md)) | ✅ Done on the production-like stack with stand-ins, on one laptop — **not on a deployment**: there is none, and none of this may be done to one | 53 unit + 9 integration |
| 32–50 | Production & Polish | ⏳ | |

**Total: 1,253 unit + contract, 74 integration, 60 browser.** Zero network calls in CI; the integration tests run every night (Phase 28). (One more browser test, `e2e-production/`, checks a running deployment and is run by hand.)

> Verified against the live APIs on 2026-10-02 (Duffel and LiteAPI in sandbox mode) — see [docs/phase1-17_audit.md](docs/phase1-17_audit.md).

---

## API Keys Required (Phase 3+)

| Variable | Service | Sign-up |
|---|---|---|
| `GOOGLE_API_KEY` | Gemini — intent parsing, refinement classifier, embeddings | https://aistudio.google.com/apikey |
| `GROQ_API_KEY` | Groq — itinerary builder, preference extraction, local tips; also answers Gemini's prompts when Gemini is unavailable | https://console.groq.com |
| `DUFFEL_ACCESS_TOKEN` | Flights (a test-mode token returns sandbox offers) | https://duffel.com |
| `LITEAPI_API_KEY` | Hotels (the free sandbox key is enough) | https://liteapi.travel |
| `OPENTRIPMAP_API_KEY` | Attractions | https://opentripmap.io |
| `OPENWEATHER_API_KEY` | Weather tool — optional: no planning step reads it yet; cache warming asks for it when a trip is created | https://openweathermap.org/api |

Five keys, all free tier. Geocoding uses Nominatim (OpenStreetMap) and needs no key — and neither
does the map in the exported PDF, which is drawn from OpenStreetMap tiles (`MAP_TILE_URL` points it
at another tile server; with the tiles unreachable the PDF simply comes without the map).

Gemini's free tier allows about 20 requests a day per model. When it runs out, the short prompts go
to Groq's small model (`GROQ_SMALL_MODEL`) instead, so planning keeps working.

**Group trips:** name the travellers and what each enjoys, and each one's interests are searched on their
own; the plan then has a stop for everyone in every two days — checked and, if need be, repaired by code —
says who each stop is for, and splits the cost equally. What the attractions provider does not have is said,
not substituted: it lists no spas, so "spa" finds nothing.

**Scope:** trips within India, up to 14 nights. A destination abroad ("London") is refused with a
clear message rather than planned as its nearest namesake.

Every provider is asked no more often than it allows (Duffel 30 searches a minute, LiteAPI 5 requests a
second, OpenTripMap 10, OpenWeather 60 a minute, Nominatim 1 a second): a request over the limit waits its
turn, and one answered 429 or 5xx is tried again after a backoff. Creating a trip starts its searches in the
background so that planning finds them cached — `CACHE_WARMING_ENABLED=false` turns that off where a quota
is too tight to spend on trips that may never be planned.

**Tracing and costs (optional, off by default):** `LANGCHAIN_TRACING_V2=true` and a `LANGCHAIN_API_KEY`
(https://smith.langchain.com) upload every run to LangSmith as a trace — each step, model call and tool call
a span, tagged with `trip_id` and `user_id`, so one trip's every decision is one filter away. A trace holds
the trip: its destination, dates, travellers' names and interests, the prompts and the plan — no keys, and
none of it leaves the machine unless `.env` says so. Without an account, `python scripts/trace_receiver.py`
prints the same traces locally (`HOW_TO_RUN.md`, Step 24). Whether or not tracing is on, each run that asked a
model records its tokens and their list price: `GET /admin/cost-summary?since=2026-10-01` adds them up by
agent and by day, and a trip whose models pass `LLM_COST_ALERT_USD` (default $0.10) is logged. The prices are
the providers' list prices as read on 2026-10-11 (LangSmith's own cost for each call agrees with them) — when one moves, correct it with `LLM_PRICES`.

**Deploying (Phase 27).** The backend is one Docker image configured by its environment (`railway.json`
tells Railway how to build, check and stop it); the frontend is `src/frontend` on Vercel. Before anything
is deployed, `PYTHONPATH=src/backend python -m app.preflight --env-file <your values> --production` says
everything that is missing or wrong, by name. Without any account, `python scripts/prodlike.py up` starts
the same image against a TLS-only Postgres behind a connection pooler and a TLS-only Redis, and
`python scripts/prod_check.py --prodlike` asks it the roadmap's questions — the same script takes a real
deployment's addresses. Step by step: `HOW_TO_RUN.md`, Step 25. Not yet done from this repository: the
deploy to the real services.

**CI/CD (Phase 28).** `.github/workflows/ci.yml` runs on every push: the `backend` job (lint, the migrations
against a real Postgres — `alembic check` fails the build when a model says something no migration does —
and the 1,253 unit and contract tests) and the `frontend` job (types, lint, a production build). On `main`
the `deploy` job follows only when both pass. It needs four things set in the repository — the secret
`RAILWAY_TOKEN` and the variables `RAILWAY_SERVICE`, `PRODUCTION_API_URL`, `PRODUCTION_WEB_URL` — and until
they are it says "Nothing was deployed" and passes. The frontend is deployed by Vercel's own GitHub
integration. `scripts/wait_for_release.py` then asks `GET /version` at the backend, the site and the site's
`/api` until all three answer with the merged commit, and writes the merge-to-live time on the run's page.
`.github/workflows/nightly.yml` runs the integration tests at 02:00 UTC and comments on the issue labelled
`nightly`. Two things are settings, not files: requiring the two checks before a merge to `main` (branch
protection — on a private repository that needs GitHub Pro), and the four values above. `HOW_TO_RUN.md`,
Step 26.

**Observability (Phase 29).** `GET /metrics` is always there — open in development, behind `OPS_TOKEN` in
production. Traces are sent when `OTEL_EXPORTER_OTLP_ENDPOINT` says where (OTLP over HTTP), and log lines
are JSON when `LOG_FORMAT=json` (the default in production); both are scrubbed of secrets like the error
reports. `python scripts/observability.py up` starts Prometheus, Grafana (http://localhost:3001), Jaeger
(http://localhost:16686), Loki and Alloy on this machine, and `python scripts/observability_check.py`
plans a trip and asks all four what they saw: the request rate, error rate and duration per endpoint,
`llm_calls_total` / `mcp_cache_hit_ratio` / `itinerary_builds_total`, one connected trace from the
request to the `INSERT` of the itinerary across both processes, and the trip's log lines from both
services. Nothing here alerts yet (Phase 34), and none of it has run against a deployment.

**Load testing (Phase 30).** `python scripts/loadtest.py k6` builds a k6 that can read an event stream;
`up` starts the deployed image on the production-like stack with stand-ins for the models and the providers
— a different program, not a setting: there is nothing to switch on in a deployment — and
`ladder --name <a name>` starts visits at a fixed rate, one rate after another, while it watches the pooler,
Postgres, the backend's `/metrics` and Docker. What it found is [docs/load-test-report.md](docs/load-test-report.md):
the stack as Phase 29 left it held one visit every ten seconds (1.3 requests a second at 210 ms p99) because
every plan in flight kept a database connection idle inside a transaction; with that fixed in the code, the
pools made settings and the pooler lending what a hosted one lends, it holds 3.2 visits a second (43 requests
a second at 206 ms p99), and what limits it then is one process on one core. All of it on one laptop that
ran both the load and the system: where the app runs out first, not what a deployment will do.
`HOW_TO_RUN.md`, Step 28.

**Chaos (Phase 31).** `python scripts/chaos.py up` is that stack with one container more, and
`python scripts/chaos.py all` breaks four things while a trip is planned: it kills the tool server, has
Gemini answer 429, has the model that writes the itinerary never answer, and kills Redis for 30 seconds under
twenty open event streams. A model goes wrong by its provider's *own client* being pointed at a server that
fails — which is how the worst of it was found: that client had no timeout at all, and one unanswered request
kept a trip "planning" for as long as the process lived. On the app as Phase 30 left it, a killed tool server
also took every later search with it until a restart, and a plan in flight took as long as a Redis outage did.
All three are fixed and each exercise now goes as [docs/chaos-report.md](docs/chaos-report.md) says it should;
Gemini's 429 needed nothing. `HOW_TO_RUN.md`, Step 29.
`HOW_TO_RUN.md`, Step 27.

All tools return `ToolError(code="API_NOT_CONFIGURED")` when keys are missing — the server never crashes.
A missing key degrades one part of the plan (no flights, no hotel, …) — it never crashes a run.
Model IDs are settings too (`GEMINI_MODEL`, `GROQ_MODEL`), so a retired model is an `.env` change.

---

## Known local-setup gotchas (fixed in requirements.txt, documented for awareness)

- **`email-validator` missing** → `pydantic`'s `EmailStr` needs this as a separate package. Already pinned in `requirements.txt`.
- **`bcrypt` version mismatch** → `passlib` can't read `__about__` from `bcrypt>=4.1`, breaks password hashing with a misleading "password too long" error. Pinned to `bcrypt==4.0.1`.
- **Docker not installed** → see Step 0 of `HOW_TO_RUN.md` for full first-time Mac setup, or install Docker Desktop directly from docker.com.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | Next.js 14, Tailwind CSS, Leaflet (react-leaflet), lucide-react icons |
| PDF export | ReportLab (pure Python) + Pillow; Inter and Fraunces embedded (Phase 19); Noto for the scripts of India, shaped by HarfBuzz (`uharfbuzz`, Phase 20) |
| Backend | FastAPI, Uvicorn, Python 3.11 |
| Auth | JWT (python-jose + passlib/bcrypt) |
| SSE | sse-starlette + Redis pub/sub |
| Agents | LangGraph, MCP SDK |
| LLMs | Gemini Flash (intent parsing, refinement), Groq gpt-oss-120b (itinerary, preference extraction, local tips) |
| Database | PostgreSQL 16 + pgvector |
| Cache | Redis 7 |
| ORM | SQLModel + Alembic |
| Embeddings | Gemini `gemini-embedding-001` at 1536 dimensions (Phase 14) |
| Observability | LangSmith traces + per-run token costs in `agent_runs` (Phase 26); Sentry, with reports scrubbed of secrets (Phase 27); Prometheus metrics, OpenTelemetry traces (Jaeger), JSON logs (Loki), Grafana (Phase 29) |
| Deploy | Railway (backend, Docker), Vercel (frontend), Neon (Postgres + pgvector, pooled), Upstash (Redis over TCP + TLS) — Phase 27 |
| CI/CD | GitHub Actions: checks on every push, deploy on `main` after them, integration tests nightly — Phase 28 |
| Load testing | k6 with the `xk6-sse` extension, an open model (visits at a fixed rate); stand-ins for the models and providers that are not in the deployed image — Phase 30 |
| Chaos | Faults made from outside the app: `kill -9`, `docker kill`, and the providers' own clients pointed at a server that fails — Phase 31 |

---

*MIT License · © 2026 Shauryaman Saxena*
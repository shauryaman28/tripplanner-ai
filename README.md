# AI Trip Planner

> Multi-agent AI travel planner — flights, hotels, activities & itineraries.
> Built with FastAPI · LangGraph · MCP · Gemini Flash · Groq gpt-oss-120b · pgvector.

**Status: Phase 23 / 50 — pgvector Similarity Search**

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
                                        MCP Server (5 tools)
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
pip install -r requirements.txt
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
# Unit + contract tests (no Docker, no network) — 696 tests
pytest tests/unit/ tests/contract/ -v

# Integration tests (Docker Postgres + Redis) — 36 tests, incl. the full
# plan → export → refine → replan pipeline through the HTTP API. They use their own
# `tripplanner_db_test` database, so dev data is never touched.
RUN_INTEGRATION=1 pytest tests/integration/ -v

# Browser tests (Playwright) — 55 tests: 5 end-to-end flows, 8 for Phase 23's similar trips and search, 10 for
# Phase 22's local tips, 3 for Phase 21's priced
# budget conflict, 5 for Phase 20's polish (live draft, refinement marks, retry, 375 px), 16 for the live draft's
# reader, 8 for the change summary. Starts its own stack: a stub backend
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
│   │   ├── next.config.mjs              ← /api/* proxy → FastAPI backend
│   │   ├── tailwind.config.ts           ← design tokens: neutrals, status colours, shadows, motion
│   │   ├── tsconfig.json
│   │   ├── playwright.config.ts         ← starts the E2E stack (stub backend :8100, dev server :3100)
│   │   ├── e2e/planning.spec.ts         ← Playwright tests: plan, map, refine, PDF download, budget conflict
│   │   ├── e2e/polish.spec.ts           ← Phase 20: live draft, refinement marks, retry, every view at 375 px
│   │   ├── e2e/budget.spec.ts           ← Phase 21: a budget conflict priced, and each way out planned
│   │   ├── e2e/tips.spec.ts             ← Phase 22: the Local tips accordion; no section when there are no tips
│   │   ├── e2e/similar.spec.ts          ← Phase 23: similar trips on the trip page; search on the trips list
│   │   ├── e2e/changes.spec.ts          ← what the assistant says changed (pure function tests)
│   │   ├── e2e/draft.spec.ts            ← the live draft's partial-JSON reader (pure function tests)
│   │   └── src/
│   │       ├── app/
│   │       │   ├── globals.css          ← component classes (.btn, .card, .pin…) and Leaflet overrides
│   │       │   ├── layout.tsx           ← fonts (Inter + Fraunces)
│   │       │   ├── page.tsx             ← redirects → /trips
│   │       │   ├── login/page.tsx       ← sign in / create account
│   │       │   └── trips/
│   │       │       ├── page.tsx         ← trip cards, new-trip form, search (Phase 23)
│   │       │       └── [id]/page.tsx    ← the plan, the assistant, live progress
│   │       ├── components/              ← ItineraryView, CostSummary, DayCard, ItineraryMap (Phase 18),
│   │       │                              DownloadPdfButton + Toast (Phase 19), LiveDraft, ProgressSheet,
│   │       │                              TripStages (Phase 20), LocalTips (Phase 22), SimilarTrips (Phase 23),
│   │       │                              AgentProgressPanel,
│   │       │                              MessageThread, ChatInput, AppHeader, Brand, ui
│   │       └── lib/                     ← api.ts, sse.ts (reconnecting EventSource), map.ts (pins, routes,
│   │                                      day colours), changes.ts (what a change request changed, and
│   │                                      which parts), draft.ts (the itinerary while it is written),
│   │                                      tips.ts (local tips), useWideScreen.ts, download.ts, places.ts,
│   │                                      format.ts, types.ts
│   ├── backend/
│   │   ├── Dockerfile
│   │   └── app/
│   │       ├── main.py                  ← FastAPI entry point
│   │       ├── api/
│   │       │   ├── deps.py              ← JWT dependencies
│   │       │   └── routes/
│   │       │       ├── auth.py          ← POST /auth/register, /auth/login
│   │       │       ├── health.py        ← GET /ping
│   │       │       ├── trips.py         ← All trip routes + SSE + GET /status + POST /retry + GET /export/pdf
│   │       │       │                              + GET /similar and GET /search (Phase 23)
│   │       │       ├── users.py         ← GET/PUT /users/preferences (Phase 16)
│   │       │       └── admin.py         ← GET /admin/embedding-health (Phase 14)
│   │       ├── core/
│   │       │   ├── config.py            ← pydantic-settings
│   │       │   └── security.py         ← JWT + password hashing
│   │       ├── db/
│   │       │   ├── session.py           ← async SQLAlchemy
│   │       │   └── redis.py             ← async Redis singleton
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
│   └── ai/
│       ├── llm.py                       ← model IDs + tolerant JSON parsing of LLM replies
│       ├── pricing.py                   ← Phase 21: seasons by destination, typical costs, the estimate arithmetic
│       ├── mcp_server/                  ← Phase 3: server, tools, models, cache
│       ├── mcp_client/                  ← Phase 6: client.py talks to the MCP server
│       ├── embeddings/                  ← Phase 14: Gemini embedding writer; Phase 23: the summary text, query embedding
│       ├── utils/
│       │   ├── run_logger.py            ← Phase 6: writes agent_runs
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
│           └── orchestrator.py          ← Phase 9–22: full graph with preference injection, refinement & loops,
│                                              builder_token streaming, retry of one search, local tips
├── migrations/                          ← Alembic migrations
│   └── versions/
│       ├── 001_initial_schema.py        ← All 5 tables + pgvector
│       ├── 002_add_turn_to_agent_runs.py← Phase 15: turn tracking
│       ├── 003_add_user_preferences.py  ← Phase 16: user_preferences table
│       └── 004_embedding_kind.py        ← Phase 23: embeddings.kind, HNSW index over summaries, re-embedding queued
├── tests/
│   ├── unit/                            ← Fast, no network, mock everything (688 tests)
│   ├── contract/                        ← Response shape tests (mocked, 8 tests)
│   ├── integration/                     ← Real Postgres + Redis (RUN_INTEGRATION=1, 36 tests)
│   ├── database.py                      ← separate test databases (<db>_test, <db>_e2e), migrated with Alembic
│   ├── e2e/stub_backend.py              ← the real app with external APIs stubbed, for Playwright
│   └── fakes.py                         ← network stubs shared by integration + E2E (APIs, LLMs, map tiles)
├── docker/
│   └── init.sql                         ← enables pgvector extension
├── scripts/                             ← run by hand: embedding_experiment.py (Phase 23's experiment, real model),
│                                          seed_demo_trips.py (a demo account with twelve embedded trips)
├── prompts/                             ← versioned LLM prompts (one file per version per agent)
├── docs/                                ← phase build logs (1–23) + phase1-17_audit.md
├── DECISIONS.md                         ← architectural decision log
├── alembic.ini
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
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
| 21–25 | Intelligence Layer | ⏳ | |
| 26–50 | Production & Polish | ⏳ | |

**Total: 457 unit + contract, 17 integration, 13 browser (5 end-to-end flows + 8 change-summary) — all passing.** Zero network calls in CI.

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
| `OPENWEATHER_API_KEY` | Weather tool — optional, not used by planning yet | https://openweathermap.org/api |

Five keys, all free tier. Geocoding uses Nominatim (OpenStreetMap) and needs no key — and neither
does the map in the exported PDF, which is drawn from OpenStreetMap tiles (`MAP_TILE_URL` points it
at another tile server; with the tiles unreachable the PDF simply comes without the map).

Gemini's free tier allows about 20 requests a day per model. When it runs out, the short prompts go
to Groq's small model (`GROQ_SMALL_MODEL`) instead, so planning keeps working.

**Scope:** trips within India, up to 14 nights. A destination abroad ("London") is refused with a
clear message rather than planned as its nearest namesake.

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
| Observability | LangSmith, Sentry (Phase 26+) |
| Deploy | Railway (backend), Vercel (frontend), Neon (DB), Upstash (Redis) |

---

*MIT License · © 2026 Shauryaman Saxena*
# AI Trip Planner

> Multi-agent AI travel planner — flights, hotels, activities & itineraries.
> Built with FastAPI · LangGraph · MCP · Gemini Flash · Groq Llama 3.3 · Claude Haiku · pgvector.

**Status: Phase 17 / 50 — Frontend: Chat Interface & SSE Streaming**

---

## Architecture

```
User → Next.js 14 → FastAPI Gateway → OrchestratorAgent (LangGraph)
                         │                      │
                    JWT auth              ┌─────┼─────────────┐
                    SSE stream            ▼     ▼             ▼
                    Redis pub/sub   FlightAgent HotelAgent ActivitiesAgent
                                          │     │             │
                                          └─────┴─────────────┘
                                                │
                                        MCP Server (5 tools)
                               Duffel · OpenTripMap · OpenWeatherMap
                                                │
                                   ItineraryBuilder (Groq Llama 3.3)
                                      → Evaluator (deterministic)
                                                │
                                      Postgres + pgvector
                                         Redis pub/sub
                                        SSE → Frontend
```

---

## Quick Start

### Prerequisites
- Docker Desktop
- Python 3.11+ (3.9+ also works for local dev; CI/Docker use 3.11)
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
# Unit + contract tests (no Docker, no network) — 288 tests
pytest tests/unit/ tests/contract/ -v

# Integration tests (Docker Postgres + Redis) — 14 tests, incl. the full
# plan → refine → replan pipeline through the HTTP API. They use their own
# `tripplanner_db_test` database, so dev data is never touched.
RUN_INTEGRATION=1 pytest tests/integration/ -v

# Browser E2E (Playwright). Starts a stub backend (real app, DB, Redis and
# graph; external APIs faked) and the Next.js dev server by itself.
cd src/frontend
npx playwright install chromium   # once
npx playwright test
```
Already have the real backend running on :8000? Playwright reuses it, and the
same spec then runs against your real API keys.

---

## Project Structure

```
tripplanner-ai/
├── src/
│   ├── frontend/                        ← Phase 17: Next.js 14 App Router
│   │   ├── package.json
│   │   ├── next.config.mjs              ← /api/* proxy → FastAPI backend
│   │   ├── tailwind.config.ts           ← brand palette: indigo + saffron
│   │   ├── tsconfig.json
│   │   ├── playwright.config.ts         ← starts stub backend + dev server
│   │   ├── e2e/planning.spec.ts         ← Playwright smoke test (Phase 17)
│   │   └── src/
│   │       ├── app/
│   │       │   ├── globals.css          ← design tokens & custom keyframes
│   │       │   ├── layout.tsx
│   │       │   ├── page.tsx             ← redirects → /trips
│   │       │   ├── login/page.tsx       ← auth login/register
│   │       │   └── trips/
│   │       │       ├── page.tsx         ← trip listing & inline creation
│   │       │       └── [id]/page.tsx    ← live chat, SSE, itinerary view
│   │       ├── components/              ← AgentProgressPanel, ChatInput, DayCard, ItineraryView
│   │       └── lib/                     ← api.ts, sse.ts (reconnecting EventSource), types.ts
│   ├── backend/
│   │   ├── Dockerfile
│   │   └── app/
│   │       ├── main.py                  ← FastAPI entry point
│   │       ├── api/
│   │       │   ├── deps.py              ← JWT dependencies
│   │       │   └── routes/
│   │       │       ├── auth.py          ← POST /auth/register, /auth/login
│   │       │       ├── health.py        ← GET /ping
│   │       │       ├── trips.py         ← All trip routes + SSE + GET /status
│   │       │       ├── users.py         ← GET/PUT /users/preferences (Phase 16)
│   │       │       └── admin.py         ← GET /admin/embedding-health (Phase 14)
│   │       ├── core/
│   │       │   ├── config.py            ← pydantic-settings
│   │       │   └── security.py         ← JWT + password hashing
│   │       ├── db/
│   │       │   ├── session.py           ← async SQLAlchemy
│   │       │   └── redis.py             ← async Redis singleton
│   │       ├── models/                  ← SQLModel table models
│   │       └── schemas/                 ← Pydantic request/response schemas
│   └── ai/
│       ├── llm.py                       ← model IDs + tolerant JSON parsing of LLM replies
│       ├── mcp_server/                  ← Phase 3: server, tools, models, cache
│       ├── mcp_client/                  ← Phase 6: client.py talks to the MCP server
│       ├── embeddings/                  ← Phase 14: OpenAI embedding writer
│       ├── utils/
│       │   ├── run_logger.py            ← Phase 6: writes agent_runs
│       │   ├── conversation.py          ← Phase 7B/15: Redis history + planning state
│       │   ├── preferences.py           ← Phase 16: preference loading / injection
│       │   └── tasks.py                 ← fire-and-forget background tasks
│       ├── agents/
│       │   ├── flight_agent.py          ← Phase 6–7: 3-node graph (parse → route → search/clarify)
│       │   ├── hotel_agent.py           ← Phase 8 Dev A: 3-node graph, hotel-specific routing
│       │   ├── activities_agent.py      ← Phase 8 Dev B: 3-node graph, dual-requirement router
│       │   ├── budget_decision.py       ← Phase 10: pure budget threshold logic
│       │   ├── evaluator.py             ← Phase 11: 4 deterministic itinerary checks + retry routing
│       │   ├── refinement_classifier.py ← Phase 15: which agents a follow-up message re-runs
│       │   └── preference_extractor.py  ← Phase 16: learning lasting preferences from trips
│       ├── builder/
│       │   └── builder.py               ← Phase 12: ItineraryBuilder (Groq Llama 3.3), data-scope + budget-math validation
│       └── orchestrator/
│           └── orchestrator.py          ← Phase 9–16: full graph with preference injection, refinement & loops
├── migrations/                          ← Alembic migrations
│   └── versions/
│       ├── 001_initial_schema.py        ← All 5 tables + pgvector
│       ├── 002_add_turn_to_agent_runs.py← Phase 15: turn tracking
│       └── 003_add_user_preferences.py  ← Phase 16: user_preferences table
├── tests/
│   ├── unit/                            ← Fast, no network, mock everything (281 tests)
│   ├── contract/                        ← Response shape tests (mocked, 7 tests)
│   ├── integration/                     ← Real Postgres + Redis (RUN_INTEGRATION=1, 14 tests)
│   ├── e2e/stub_backend.py              ← the real app with external APIs stubbed, for Playwright
│   └── fakes.py                         ← network stubs shared by integration + E2E
├── docker/
│   └── init.sql                         ← enables pgvector extension
├── prompts/                             ← versioned LLM prompts (one file per version per agent)
├── docs/                                ← phase build logs (1–17) + phase1-17_audit.md
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
| 3 | MCP Server (real APIs: Duffel, OpenTripMap, OWM) | ✅ Done | 31 tool + 7 contract tests |
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
| 14 | Embedding Generation (OpenAI) | ✅ Done | 21 unit + 4 integration |
| 15 | Multi-Turn Refinement | ✅ Done | 27 unit tests |
| 16 | User Preferences & Personalisation | ✅ Done | 56 unit tests |
| 17 | Frontend: Chat Interface & SSE Streaming | ✅ Done | 6 status unit + 3 Playwright E2E |
| 1–17 | End-to-end audit ([docs/phase1-17_audit.md](docs/phase1-17_audit.md)) | ✅ Done | 26 regression unit + 5 pipeline/schema integration |
| 18–20 | Frontend: Map & Polishing | ⏳ | |
| 21–25 | Intelligence Layer | ⏳ | |
| 26–50 | Production & Polish | ⏳ | |

**Total: 288 unit + contract, 14 integration, 3 browser E2E — all passing.** Zero network calls in CI.

> ⚠️ Not yet verified against live APIs: see "Still not verified" in [docs/phase1-17_audit.md](docs/phase1-17_audit.md). Hotels currently have no provider.

---

## API Keys Required (Phase 3+)

| Variable | Service | Sign-up |
|---|---|---|
| `GOOGLE_API_KEY` | Intent parsing + refinement classifier (Gemini Flash) | https://aistudio.google.com/apikey |
| `GROQ_API_KEY` | Itinerary Builder (Llama 3.3) | https://console.groq.com |
| `DUFFEL_ACCESS_TOKEN` | Flights (test-mode token is enough) | https://duffel.com |
| `OPENTRIPMAP_API_KEY` | Attractions | https://opentripmap.io |
| `OPENWEATHER_API_KEY` | Weather | https://openweathermap.org/api |
| `OPENAI_API_KEY` | Embeddings — optional | https://platform.openai.com |
| `ANTHROPIC_API_KEY` | Preference extraction — optional | https://console.anthropic.com |

**Hotels:** Amadeus closed its self-service portal on 2026-07-17 and no replacement is wired in yet.
`search_hotels` returns `API_NOT_CONFIGURED` and trips are planned without a hotel.

All tools return `ToolError(code="API_NOT_CONFIGURED")` when keys are missing — the server never crashes.
A missing optional key degrades one feature (embeddings are queued as `pending_retry`; preferences fall back to a heuristic) — it never fails a trip.
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
| Frontend | Next.js 14, Tailwind CSS (Leaflet map: Phase 18) |
| Backend | FastAPI, Uvicorn, Python 3.11 |
| Auth | JWT (python-jose + passlib/bcrypt) |
| SSE | sse-starlette + Redis pub/sub |
| Agents | LangGraph, MCP SDK |
| LLMs | Gemini Flash (intent parsing, refinement), Groq Llama 3.3 (itinerary), Claude Haiku 4.5 (preference extraction) |
| Database | PostgreSQL 16 + pgvector |
| Cache | Redis 7 |
| ORM | SQLModel + Alembic |
| Embeddings | OpenAI text-embedding-3-small (Phase 14) |
| Observability | LangSmith, Sentry (Phase 26+) |
| Deploy | Railway (backend), Vercel (frontend), Neon (DB), Upstash (Redis) |

---

*MIT License · © 2026 Shauryaman Saxena*
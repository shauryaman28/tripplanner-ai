# AI Trip Planner

> Multi-agent AI travel planner — flights, hotels, activities & itineraries.
> Built with FastAPI · LangGraph · MCP · Claude Haiku · Gemini Flash · pgvector.

**Status: Phase 12 / 50 — Agent Core (Itinerary Builder: Claude Haiku)**

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
                                     Amadeus · GMaps · OWM
                                                │
                                      ItineraryBuilder (Claude Haiku)
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
uvicorn app.main:app --reload
```

### 5. Verify Phase 1 done criterion
```bash
curl http://localhost:8000/ping
# → {"postgres": "ok", "redis": "ok"}
```

### 6. Test auth (Phase 5)
```bash
# Register
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"yourpassword"}'

# Login → get token
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -d "username=you@example.com&password=yourpassword" | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# Create trip
curl -X POST http://localhost:8000/trips \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"destination":"Goa","start_date":"2025-12-10","end_date":"2025-12-17","budget":50000}'
```

### 7. Run MCP server (Phase 3)
```bash
python -m src.ai.mcp_server.server

# Inspect via MCP Inspector:
npx @modelcontextprotocol/inspector python -m src.ai.mcp_server.server
```

### 8. Run tests
```bash
# Unit + contract tests (no Docker required) — 82 tests
pytest tests/unit/ tests/contract/ -v

# Integration tests (Docker must be running)
RUN_INTEGRATION=1 pytest tests/integration/ -v
```

---

## Project Structure

```
tripplanner-ai/
├── src/
│   ├── backend/
│   │   ├── Dockerfile
│   │   └── app/
│   │       ├── main.py                  ← FastAPI entry point
│   │       ├── api/
│   │       │   ├── deps.py              ← JWT dependencies
│   │       │   └── routes/
│   │       │       ├── auth.py          ← POST /auth/register, /auth/login
│   │       │       ├── health.py        ← GET /ping
│   │       │       └── trips.py         ← All trip routes + SSE
│   │       ├── core/
│   │       │   ├── config.py            ← pydantic-settings
│   │       │   └── security.py         ← JWT + password hashing
│   │       ├── db/
│   │       │   ├── session.py           ← async SQLAlchemy
│   │       │   └── redis.py             ← async Redis singleton
│   │       ├── models/                  ← SQLModel table models
│   │       └── schemas/                 ← Pydantic request/response schemas
│   └── ai/
│       ├── mcp_server/                  ← Phase 3: server, tools, models, cache
│       ├── mcp_client/                  ← Phase 6: client.py talks to the MCP server
│       ├── utils/
│       │   ├── run_logger.py            ← Phase 6: writes agent_runs
│       │   └── conversation.py          ← Phase 7B: Redis history + state helpers
│       ├── agents/
│       │   ├── flight_agent.py          ← Phase 6–7: 3-node graph (parse → route → search/clarify)
│       │   ├── hotel_agent.py           ← Phase 8 Dev A: 3-node graph, hotel-specific routing
│       │   ├── activities_agent.py      ← Phase 8 Dev B: 3-node graph, dual-requirement router
│       │   ├── budget_decision.py       ← Phase 10: pure budget threshold logic
│       │   └── evaluator.py             ← Phase 11: 4 deterministic itinerary checks + retry routing
│       ├── builder/
│       │   └── builder.py               ← Phase 12: ItineraryBuilder (Claude Haiku), data-scope + budget-math validation
│       └── orchestrator/
│           └── orchestrator.py          ← Phase 9–12: full graph with build/evaluate/retry/persist loop
├── migrations/                          ← Alembic migrations
│   └── versions/
│       └── 001_initial_schema.py        ← All 5 tables + pgvector
├── tests/
│   ├── unit/                            ← Fast, no network, mock everything
│   ├── contract/                        ← Response shape tests (mocked)
│   ├── integration/                     ← Real Docker (RUN_INTEGRATION=1)
│   └── e2e/                             ← Playwright (Phase 17)
├── docker/
│   └── init.sql                         ← enables pgvector extension
├── prompts/                             ← versioned LLM prompts (v1–v3 per agent)
├── docs/                                ← phase build logs (1–12)
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
| 3 | MCP Server (real APIs: Amadeus, GMaps, OWM) | ✅ Done | 7 contract tests |
| 4 | Database schema & migrations | ✅ Done | — |
| 5 | FastAPI gateway, JWT auth, SSE skeleton | ✅ Done | 5 auth + 6 trip + 4 health |
| 6 | FlightAgent: one agent, one tool | ✅ Done | 6 agent + 4 MCP client + 2 logger |
| 7 | Conditional edges: ask instead of assume | ✅ Done | 8 router + 5 clarification API |
| 8 | HotelAgent & ActivitiesAgent | ✅ Done | 6 hotel + 6 activities |
| 9 | Orchestrator: Decomposition & Fan-Out | ✅ Done | 5 orchestrator |
| 10 | Budget Conflict & Re-Planning | ✅ Done | 10 budget + 9 orchestrator |
| 11 | Evaluator Agent: Self-Checking | ✅ Done | 18 evaluator + 1 retry-chain |
| 12 | Itinerary Builder | ✅ Done | 6 builder + 8 orchestrator (new) + 2 integration |
| 13–20 | Storage & Frontend | ⏳ | |
| 21–25 | Intelligence Layer | ⏳ | |
| 26–50 | Production & Polish | ⏳ | |

**Total: 132 tests passing** (unit + contract), 137 with integration tests. Zero network calls in CI.

---

## API Keys Required (Phase 3+)

| Variable | Service | Sign-up |
|---|---|---|
| `AMADEUS_CLIENT_ID` + `AMADEUS_CLIENT_SECRET` | Flights + Hotels | https://developers.amadeus.com |
| `OPENTRIPMAP_API_KEY` | Attractions | https://opentripmap.io |
| `OPENWEATHER_API_KEY` | Weather | https://openweathermap.org/api |
| `GROQ_API_KEY` | Itinerary Builder (Llama 3.3) | https://console.groq.com |
| `GOOGLE_API_KEY` | Agents (Gemini Flash) | https://aistudio.google.com/apikey |

All tools return `ToolError(code="API_NOT_CONFIGURED")` when keys are missing — the server never crashes.
All keys above are available on free tiers with no credit card required.

---

## Known local-setup gotchas (fixed in requirements.txt, documented for awareness)

- **`email-validator` missing** → `pydantic`'s `EmailStr` needs this as a separate package. Already pinned in `requirements.txt`.
- **`bcrypt` version mismatch** → `passlib` can't read `__about__` from `bcrypt>=4.1`, breaks password hashing with a misleading "password too long" error. Pinned to `bcrypt==4.0.1`.
- **Docker not installed** → see `docs/local-setup.md` (Phase 46) for full first-time Mac setup, or install Docker Desktop directly from docker.com.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | Next.js 14, Tailwind CSS, shadcn/ui, Leaflet |
| Backend | FastAPI, Uvicorn, Python 3.11 |
| Auth | JWT (python-jose + passlib/bcrypt) |
| SSE | sse-starlette + Redis pub/sub |
| Agents | LangGraph, MCP SDK |
| LLMs | Gemini Flash (orchestration), Claude Haiku 4.5 (itinerary) |
| Database | PostgreSQL 16 + pgvector |
| Cache | Redis 7 |
| ORM | SQLModel + Alembic |
| Embeddings | OpenAI text-embedding-3-small (Phase 14) |
| Observability | LangSmith, Sentry |
| Deploy | Railway (backend), Vercel (frontend), Neon (DB), Upstash (Redis) |

---

*MIT License · © 2026 Shauryaman Saxena*
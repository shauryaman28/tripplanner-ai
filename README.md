# AI Trip Planner

> Multi-agent AI travel planner — flights, hotels, activities & itineraries.
> Built with FastAPI · LangGraph · MCP · Claude Haiku · Gemini Flash · pgvector.

**Status: Phase 6 / 30 — Agent Core (FlightAgent)**

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
# Unit + contract tests (no Docker required)
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
│       ├── utils/                       ← Phase 6: run_logger.py writes agent_runs
│       ├── agents/                      ← Phase 6–8 (LangGraph agents)
│       ├── builder/                     ← Phase 12 (ItineraryBuilder)
│       └── orchestrator/                ← Phase 9 (OrchestratorAgent)
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
├── prompts/                             ← versioned LLM prompts (Phase 6+)
├── docs/                                ← phase build logs
├── alembic.ini
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

---

## Phase Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Repo & Local Infrastructure | ✅ Done |
| 2 | MCP Server (protocol + mocked tools) | ✅ Done |
| 3 | MCP Server (real APIs: Amadeus, GMaps, OWM) | ✅ Done |
| 4 | Database schema & migrations | ✅ Done |
| 5 | FastAPI gateway, JWT auth, SSE skeleton | ✅ Done |
| 6 | FlightAgent — Dev A: LangGraph node + stub MCP client ✅ · Dev B: real MCP client + run logger | 🟡 In progress |
| 7–12 | Agent Core (remaining) | ⏳ |
| 13–20 | Storage & Frontend | ⏳ |
| 21–25 | Intelligence Layer | ⏳ |
| 26–30 | Production Readiness | ⏳ |

---

## API Keys Required (Phase 3+)

| Variable | Service | Sign-up |
|---|---|---|
| `AMADEUS_CLIENT_ID` + `AMADEUS_CLIENT_SECRET` | Flights + Hotels | https://developers.amadeus.com |
| `GOOGLE_MAPS_API_KEY` | Attractions | https://console.cloud.google.com |
| `OPENWEATHER_API_KEY` | Weather | https://openweathermap.org/api |

All tools return `ToolError(code="API_NOT_CONFIGURED")` when keys are missing — the server never crashes.

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
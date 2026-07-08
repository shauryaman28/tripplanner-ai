# AI Trip Planner

> Multi-agent AI travel planner — flights, hotels, activities & itineraries.
> Built with FastAPI · LangGraph · MCP · Claude Haiku · Gemini Flash · pgvector.

**Status: Phase 1 / 30 — Infrastructure & Health Check**

---

## Architecture (evolves across 30 phases)

```
User → Next.js 14 → FastAPI Gateway → OrchestratorAgent (LangGraph)
                                              │
                        ┌─────────────────────┼─────────────────────┐
                        ▼                     ▼                     ▼
                  FlightAgent          HotelAgent          ActivitiesAgent
                        │                     │                     │
                        └─────────────────────┴─────────────────────┘
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

## Quick Start (Phase 1)

### Prerequisites
- Docker Desktop
- Python 3.11+
- Poetry (`pip install poetry`)

### 1. Clone & configure
```bash
git clone https://github.com/shauryaman28/tripplanner-ai.git
cd tripplanner-ai
cp .env.example .env
# Fill in your API keys in .env (most are optional until later phases)
```

### 2. Start infrastructure
```bash
docker compose up postgres redis -d
```

### 3. Run the backend
```bash
cd src/backend
poetry install
uvicorn app.main:app --reload
```

### 4. Verify Phase 1 is done
```bash
curl http://localhost:8000/ping
# Expected: {"postgres": "ok", "redis": "ok"}
```

### 5. Run tests
```bash
# Unit tests (no Docker required)
poetry run pytest tests/unit/ -v

# Integration tests (Docker must be running)
RUN_INTEGRATION=1 poetry run pytest tests/integration/ -v
```

---

## Project Structure

```
tripplanner-ai/
├── src/
│   ├── frontend/          ← Next.js 14  (Phase 17)
│   ├── backend/           ← FastAPI gateway
│   │   └── app/
│   │       ├── api/routes/    ← HTTP endpoints
│   │       ├── core/          ← config, settings
│   │       ├── db/            ← session, redis client
│   │       ├── models/        ← SQLModel ORM models  (Phase 4)
│   │       └── schemas/       ← Pydantic request/response schemas
│   ├── ai/
│   │   ├── orchestrator/  ← OrchestratorAgent  (Phase 9)
│   │   ├── agents/        ← Flight/Hotel/Activities agents  (Phase 6–8)
│   │   └── builder/       ← ItineraryBuilder  (Phase 12)
│   └── db/
│       └── migrations/    ← Alembic  (Phase 4)
├── tests/
│   ├── unit/              ← Fast, no network, mock everything
│   ├── integration/       ← Real Docker services (RUN_INTEGRATION=1)
│   ├── contract/          ← API shape tests  (Phase 3)
│   └── e2e/               ← Playwright  (Phase 17)
├── docker/
│   ├── docker-compose.yml ← (reference copy)
│   └── init.sql           ← enables pgvector extension
├── prompts/               ← versioned LLM prompts  (Phase 6+)
├── .github/workflows/     ← CI/CD
├── docker-compose.yml     ← bring up everything
├── pyproject.toml
└── .env.example
```

---

## Phase Progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Repo & Local Infrastructure | ✅ Done |
| 2 | MCP Server (mocked) | 🔜 Next |
| 3 | MCP Server (real APIs) | ⏳ |
| 4 | Database schema & migrations | ⏳ |
| 5 | FastAPI gateway, auth, SSE skeleton | ⏳ |
| 6–12 | Agent Core (LangGraph) | ⏳ |
| 13–20 | Storage & Frontend | ⏳ |
| 21–25 | Intelligence Layer | ⏳ |
| 26–30 | Production Readiness | ⏳ |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Next.js 14, Tailwind CSS, shadcn/ui, Leaflet |
| Backend | FastAPI, Uvicorn, Python 3.11 |
| Agents | LangGraph, MCP SDK |
| LLMs | Gemini Flash (orchestration), Claude Haiku (itinerary) |
| Database | PostgreSQL 16 + pgvector |
| Cache | Redis 7 |
| ORM | SQLAlchemy (async) + Alembic |
| Observability | LangSmith, Sentry |
| Deploy | Railway (backend), Vercel (frontend), Neon (DB), Upstash (Redis) |

---

*MIT License · © 2026 Shauryaman Saxena*

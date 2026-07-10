# AI Trip Planner

> Multi-agent AI travel planner — flights, hotels, activities & itineraries.
> Built with FastAPI · LangGraph · MCP · Claude Haiku · Gemini Flash · pgvector.

**Status: Phase 2 / 30 — MCP Server (mocked)**

---

## Architecture

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

## Quick Start

### Prerequisites
- Docker Desktop
- Python 3.11+

### 1. Clone & configure
```bash
git clone https://github.com/shauryaman28/tripplanner-ai.git
cd tripplanner-ai
cp .env.example .env
```

### 2. Start infrastructure
```bash
docker compose up postgres redis -d
```

### 3. Run the backend
```bash
cd src/backend
pip install -r ../../requirements.txt
uvicorn app.main:app --reload
```

### 4. Verify Phase 1 done criterion
```bash
curl http://localhost:8000/ping
# → {"postgres": "ok", "redis": "ok"}
```

### 5. Run MCP server (Phase 2)
```bash
python -m src.ai.mcp_server.server

# Inspect via MCP Inspector:
npx @modelcontextprotocol/inspector python -m src.ai.mcp_server.server
```

### 6. Run tests
```bash
# Unit tests (no Docker required)
pytest tests/unit/ -v

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
│   │       ├── main.py              ← FastAPI entry point
│   │       ├── api/routes/health.py ← GET /ping
│   │       ├── core/config.py       ← pydantic-settings
│   │       └── db/                  ← session.py, redis.py
│   └── ai/
│       ├── mcp_server/              ← Phase 2: server, tools, models
│       ├── agents/                  ← Phase 6–8 (LangGraph agents)
│       ├── builder/                 ← Phase 12 (ItineraryBuilder)
│       └── orchestrator/            ← Phase 9 (OrchestratorAgent)
├── tests/
│   ├── unit/                        ← Fast, no network, mock everything
│   ├── integration/                 ← Real Docker (RUN_INTEGRATION=1)
│   ├── contract/                    ← API shape tests (Phase 3)
│   └── e2e/                         ← Playwright (Phase 17)
├── docker/
│   └── init.sql                     ← enables pgvector extension
├── prompts/                         ← versioned LLM prompts (Phase 6+)
├── docs/                            ← phase build logs
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
| 2 | MCP Server (mocked) | ✅ Done |
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
|-------|------------|
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

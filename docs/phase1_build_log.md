# Phase 1 — Repo & Local Infrastructure

**Status: ✅ Complete**
**Done criterion:** `docker compose up` → `curl /ping` → `{ "postgres": "ok", "redis": "ok" }`

---

## What was built

### Repo structure

```
tripplanner-ai/
├── src/
│   ├── backend/
│   │   ├── Dockerfile
│   │   └── app/
│   │       ├── main.py              ← FastAPI entry point
│   │       ├── api/routes/health.py ← GET /ping
│   │       ├── core/config.py       ← pydantic-settings
│   │       └── db/
│   │           ├── session.py       ← async SQLAlchemy
│   │           └── redis.py         ← async Redis client
│   ├── ai/
│   │   ├── agents/      (empty — Phase 6+)
│   │   ├── builder/     (empty — Phase 12+)
│   │   └── orchestrator/ (empty — Phase 9+)
│   └── frontend/        (empty — Phase 17+)
├── docker/
│   ├── docker-compose.yml   ← Postgres-only compose (reference)
│   └── init.sql             ← CREATE EXTENSION IF NOT EXISTS vector
├── .github/workflows/
│   └── ci.yml               ← lint + unit tests on every push
├── tests/
│   ├── unit/
│   │   ├── conftest.py          ← shared fixtures (mock Redis, mock DB)
│   │   └── test_health.py       ← 4 unit tests for /ping
│   ├── integration/
│   │   └── test_health_integration.py  ← real Docker test (gated)
│   ├── contract/    (empty — Phase 3+)
│   └── e2e/         (empty — Phase 17+)
├── docker-compose.yml   ← root compose (Postgres + Redis + Backend)
├── .env.example         ← all Phase 1–27 keys pre-mapped
├── requirements.txt
├── requirements-dev.txt
└── pyproject.toml
```

---

## Key decisions and why

**`pgvector/pgvector:pg16` image from day one**
The roadmap says to enable pgvector before any agent code touches the DB. Using the dedicated image plus `docker/init.sql` means `CREATE EXTENSION vector` runs automatically on first container init. No migration needed later.

**`docker/init.sql` mounted as an init script**
```sql
CREATE EXTENSION IF NOT EXISTS vector;
```
Idempotent — safe to re-run. Mounted at `/docker-entrypoint-initdb.d/init.sql` so Postgres runs it automatically on first startup.

**Root `docker-compose.yml` uses `pgvector/pgvector:pg16`; `docker/docker-compose.yml` uses `postgres:16-alpine`**
The `docker/` version is a minimal reference compose (no backend service). The root compose is the one you actually run — it includes the backend service and the pgvector image.

**Async DB + Redis from the start**
Both `session.py` and `redis.py` are fully async. `session.py` uses `asyncpg` driver (via `postgresql+asyncpg://` URL conversion). `redis.py` uses `redis.asyncio`. No sync code to migrate later.

**Lifespan-managed Redis singleton**
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()
    yield
    await close_redis()
```
Redis is initialised once at startup, closed cleanly at shutdown. Tests mock `redis_client` directly without needing a real server.

**`get_db_raw()` as a separate function from `get_db()`**
`get_db_raw()` just runs `SELECT 1` — used only by `/ping`. Tests can mock this single function cleanly without touching the full session dependency. `get_db()` is the normal FastAPI dependency for future routes.

**CORS is environment-aware**
```python
allow_origins=["*"] if settings.APP_ENV == "development" else []
```
Dev allows all origins. Prod will restrict to the Vercel domain (Phase 27).

**`.env.example` is forward-looking**
All API keys through Phase 27 are documented and annotated with the phase they become relevant (`# Phase 6+`, `# Phase 26`). Fill in as you go — nothing is needed until that phase.

---

## Files in detail

### `src/backend/app/main.py`
FastAPI app with lifespan, CORS middleware, and health router. Future routers are commented in with their phase numbers.

### `src/backend/app/api/routes/health.py`
```
GET /ping
→ 200 { "postgres": "ok", "redis": "ok" }   both services healthy
→ 503 { "detail": "Postgres unreachable: …" }  DB down
→ 503 { "detail": "Redis unreachable: …" }     Redis down
```
Checks Postgres via `SELECT 1`, Redis via `PING`. Each check is independent — the error message names which service failed.

### `src/backend/app/core/config.py`
`pydantic-settings` `BaseSettings` subclass. Reads from `.env`, ignores unknown vars (`extra="ignore"`). All env vars typed with defaults. Single `settings` instance imported everywhere.

### `src/backend/app/db/session.py`
Async SQLAlchemy engine + session factory. `pool_pre_ping=True` recycles stale connections. SQL echo only in dev. `get_db()` is the FastAPI dependency; `get_db_raw()` is the ping-only helper.

### `src/backend/app/db/redis.py`
Module-level singleton `redis_client`, initialised by lifespan. `get_redis()` raises a clear `RuntimeError` if called before startup — avoids silent `None` bugs.

### `.github/workflows/ci.yml`
Runs on every push to any branch and on PRs to `main`:
1. Lint with `ruff check src/ tests/`
2. `pytest tests/unit/ -v` (no Docker needed)

Integration tests are commented out — run manually with `RUN_INTEGRATION=1`.

---

## Tests

### Unit tests (`tests/unit/test_health.py`) — 4 tests, no Docker

| Test | What it checks |
|---|---|
| `test_ping_returns_200_with_ok_status` | Happy path — both mocked → 200 with correct JSON |
| `test_ping_returns_503_when_postgres_down` | `get_db_raw` raises → 503, "Postgres" in detail |
| `test_ping_returns_503_when_redis_down` | `redis.ping()` raises → 503, "Redis" in detail |
| `test_ping_response_shape_is_exact` | Response keys are exactly `{"postgres", "redis"}` |

Mocking strategy: `patch("app.api.routes.health.get_db_raw")` and `patch("app.api.routes.health.get_redis")` — no real DB or Redis needed.

### Integration test (`tests/integration/test_health_integration.py`) — 1 test, requires Docker

Skipped automatically unless `RUN_INTEGRATION=1` is set. Hits real Postgres and Redis via the ASGI transport. Asserts both values are `"ok"`.

```bash
# Run manually after docker compose up
RUN_INTEGRATION=1 pytest tests/integration/ -v
```

### `tests/unit/conftest.py`
Shared fixtures: `mock_redis` (AsyncMock with sensible defaults), `mock_db_session`, `test_client` (AsyncClient with both services patched).

---

## How to run

```bash
# 1. Clone and configure
git clone https://github.com/shauryaman28/tripplanner-ai.git
cd tripplanner-ai
cp .env.example .env

# 2. Start infrastructure
docker compose up postgres redis -d

# 3. Run backend (local, hot-reload)
cd src/backend
poetry install
uvicorn app.main:app --reload

# 4. Verify done criterion
curl http://localhost:8000/ping
# → {"postgres": "ok", "redis": "ok"}

# 5. Unit tests (no Docker needed)
poetry run pytest tests/unit/ -v

# 6. Integration test (Docker must be running)
RUN_INTEGRATION=1 poetry run pytest tests/integration/ -v
```

Or run everything in Docker:
```bash
docker compose up
curl http://localhost:8000/ping
```

---

## Phase 1 done criterion

- [x] `docker compose up` starts Postgres, Redis, and Backend with health checks
- [x] `curl /ping` returns `{ "postgres": "ok", "redis": "ok" }`
- [x] 503 with specific message if either service is down
- [x] 4 unit tests pass without Docker
- [x] Integration test passes with Docker running
- [x] CI runs lint + unit tests on every push

---

## What's next — Phase 2

Build the MCP server with all 5 tools mocked. Verify every tool via the MCP Inspector before writing any agent code.

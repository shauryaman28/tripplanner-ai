# Phase 1 — Repo & Local Infrastructure

**Status: ✅ Complete**
**Done criterion:** `docker compose up` → `curl /ping` → `{ "postgres": "ok", "redis": "ok" }`

## What was built

- FastAPI app with `/ping` health check endpoint
- Async SQLAlchemy + asyncpg for Postgres
- Async Redis client (singleton via lifespan)
- `docker-compose.yml` bringing up Postgres (pgvector), Redis, and the backend
- `docker/init.sql` enabling the `vector` extension on first Postgres init
- 4 unit tests + 1 integration test for `/ping`
- GitHub Actions CI: lint + unit tests on every push

## Key decisions

**pgvector from day one** — using `pgvector/pgvector:pg16` image + `init.sql` so Phase 14 embeddings need no schema changes.

**Async throughout** — `session.py` uses `asyncpg`, `redis.py` uses `redis.asyncio`. No sync code to migrate later.

**`get_db_raw()` separate from `get_db()`** — `/ping` uses a lightweight `SELECT 1` helper. Tests mock just this function without touching the full session dependency.

**CORS is environment-aware** — `allow_origins=["*"]` in dev, locked down in prod (Phase 27).

## Done criterion checklist

- [x] `docker compose up` starts Postgres, Redis, Backend with health checks
- [x] `curl /ping` returns `{ "postgres": "ok", "redis": "ok" }`
- [x] 503 with specific message if either service is down
- [x] 4 unit tests pass without Docker
- [x] Integration test passes with Docker running
- [x] CI runs lint + unit tests on every push

# Phase 1 — Repo & Local Infrastructure

**Status: ✅ Complete**
**Done criterion:** `docker compose up` → `curl /ping` → `{ "postgres": "ok", "redis": "ok" }`

## What was built

- FastAPI app with `/ping` health check endpoint
- Async SQLAlchemy + asyncpg for Postgres
- Async Redis client (singleton via lifespan)
- `docker-compose.yml` bringing up Postgres (pgvector), Redis, and optionally the backend
- `docker/init.sql` enabling the `vector` extension on first Postgres init
- 4 unit tests + 1 integration test for `/ping`
- GitHub Actions CI: lint + unit tests on every push

## Key decisions

**pgvector from day one** — using `pgvector/pgvector:pg16` image + `init.sql` so Phase 14 embeddings need no schema changes.

**Async throughout** — `session.py` uses `asyncpg`, `redis.py` uses `redis.asyncio`. No sync code to migrate later.

**`get_db_raw()` separate from `get_db()`** — `/ping` uses a lightweight `SELECT 1` helper. Tests mock just this function without touching the full session dependency.

**Errors in body, not HTTP status** — `/ping` always returns `200`. When a service is down the value for that key is `"error: <message>"` instead of `"ok"`. This matches the roadmap spec and avoids forcing callers to handle both 200 and 503 shapes. The 4 unit tests verify both the happy path and the error-in-body behaviour.

**CORS is environment-aware** — `allow_origins=["*"]` in dev, locked down in prod (Phase 27).

## Done criterion checklist

- [x] `docker compose up postgres redis` starts Postgres and Redis with health checks
- [x] `curl /ping` returns `{ "postgres": "ok", "redis": "ok" }`
- [x] 200 with error value in body if either service is down (e.g. `{ "postgres": "error: ...", "redis": "ok" }`)
- [x] 4 unit tests pass without Docker
- [x] Integration test passes with Docker running
- [x] CI runs lint + unit tests on every push

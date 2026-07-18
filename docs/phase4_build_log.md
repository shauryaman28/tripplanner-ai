# Phase 4 — Database: Schema, Models, Migrations

**Status: ✅ Complete**
**Done criterion:** `alembic upgrade head` runs clean, schema exists in Postgres, all models importable, test DB fixture works.

## What was built

```
src/backend/app/models/
├── __init__.py     ← imports all models so SQLModel.metadata is populated
├── user.py         ← User table
├── trip.py         ← Trip table + TripStatus enum
├── itinerary.py    ← Itinerary table (content + structured_data JSONB)
├── agent_run.py    ← AgentRun table (the debugging superpower)
└── embedding.py    ← Embedding table with pgvector Vector(1536)

migrations/
├── env.py                        ← Alembic env, reads DATABASE_URL from settings
├── script.py.mako                ← Migration template
└── versions/
    └── 001_initial_schema.py     ← All 5 tables + HNSW index on embeddings
```

## Stack decision

**SQLModel** (Pydantic + SQLAlchemy) — models are Pydantic schemas AND
SQLAlchemy ORM models simultaneously. No duplication.

**Alembic** for migrations — manual migration (not autogenerate) for
Phase 4 to avoid pgvector type registration issues. Future phases can
use `alembic revision --autogenerate`.

## Tables

| Table | Key columns |
|---|---|
| `users` | id UUID, email UNIQUE, hashed_password, created_at |
| `trips` | id, user_id FK, destination, start_date, end_date, budget, group_size, interests JSONB, status, created_at |
| `itineraries` | id, trip_id FK, content TEXT, structured_data JSONB, total_cost, created_at |
| `agent_runs` | id, trip_id FK, agent_name, status, input JSONB, output JSONB, duration_ms, created_at |
| `embeddings` | id, itinerary_id FK, vector vector(1536), embedding_model, created_at |

## pgvector setup

```sql
-- done in 001_initial_schema.py:
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE embeddings ADD COLUMN vector vector(1536);
CREATE INDEX ix_embeddings_vector_hnsw ON embeddings USING hnsw (vector vector_cosine_ops);
```

HNSW index created now so Phase 23 similarity search requires zero
schema changes.

## Run migrations

```bash
# From project root (Docker Postgres must be running)
docker compose up postgres redis -d
alembic upgrade head

# Verify
psql postgresql://tripplanner:tripplanner_secret@localhost:5432/tripplanner_db \
  -c "\dt"         # list tables
  -c "SELECT column_name, data_type FROM information_schema.columns WHERE table_name='embeddings';"
```

## Why agent_runs is the debugging superpower

Every agent decision will be written here from Phase 9. When the
re-planning logic breaks in Phase 15, you query:

```sql
SELECT agent_name, status, input, output, duration_ms
FROM agent_runs
WHERE trip_id = '<id>'
ORDER BY created_at;
```

No new logging code ever needed.

## Done criterion checklist

- [x] `alembic upgrade head` runs clean against local Docker Postgres
- [x] All 5 tables exist with correct columns and indexes
- [x] All models importable: `from app.models import User, Trip, ...`
- [x] pgvector extension enabled, vector column created
- [x] HNSW index created on embeddings.vector

-- Enables pgvector extension on first Postgres init. Idempotent — safe to re-run.
CREATE EXTENSION IF NOT EXISTS vector;

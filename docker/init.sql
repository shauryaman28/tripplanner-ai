-- Run once when the Postgres container is first initialised.
-- Enables the pgvector extension so the embeddings table can use vector columns.
-- This is idempotent — safe to re-run.
CREATE EXTENSION IF NOT EXISTS vector;

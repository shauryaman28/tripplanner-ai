"""Embeddings told apart by kind, and made again — Phase 23 similarity search

Revision ID: 004
Revises: 003
Create Date: 2026-10-05

Phase 14 wrote two vectors per itinerary — its full text and a summary — with
nothing to say which was which. Phase 23 searches the summaries only, so:

  1. `kind` ("summary" | "full_text") is added;
  2. the vectors stored so far are dropped. They cannot be told apart reliably,
     and the summaries among them are of a text that ranks trips badly
     (DECISIONS #140). Each trip's latest itinerary — the only one searched —
     gets a "pending_retry" row instead: the app makes its embeddings again the
     next time it starts (main.py, startup recovery);
  3. the HNSW index over every vector is replaced by one over the summaries.
"""

import sqlalchemy as sa
from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("embeddings", sa.Column("kind", sa.String(20), nullable=True))

    op.execute(
        """
        INSERT INTO embeddings (id, itinerary_id, embedding_model, created_at)
        SELECT gen_random_uuid(), latest.id, 'pending_retry', now() AT TIME ZONE 'utc'
        FROM (SELECT DISTINCT ON (trip_id) id FROM itineraries ORDER BY trip_id, created_at DESC) AS latest
        WHERE NOT EXISTS (
            SELECT 1 FROM embeddings pending
            WHERE pending.itinerary_id = latest.id AND pending.embedding_model = 'pending_retry'
        )
        """
    )
    op.execute("DELETE FROM embeddings WHERE embedding_model <> 'pending_retry'")

    op.execute("DROP INDEX IF EXISTS ix_embeddings_vector_hnsw")
    op.execute(
        "CREATE INDEX ix_embeddings_summary_hnsw ON embeddings "
        "USING hnsw (vector vector_cosine_ops) WHERE kind = 'summary'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_embeddings_summary_hnsw")
    op.execute("CREATE INDEX ix_embeddings_vector_hnsw ON embeddings USING hnsw (vector vector_cosine_ops)")
    op.drop_column("embeddings", "kind")

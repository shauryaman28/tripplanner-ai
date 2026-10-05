"""Embedding table — pgvector storage for itinerary similarity and search (Phases 14, 23).

Two rows per itinerary are written (Phase 14), told apart by `kind` (Phase 23):
  summary    — what kind of trip it is: interests, kinds of places, places.
               What similarity and search compare (src/ai/embeddings/embedder.py).
  full_text  — every slot and hotel. Kept for comparison; nothing reads it.

The vector column uses pgvector's Vector(1536) type which maps to the
PostgreSQL 'vector' type added by the pgvector extension.
Dimension 1536 = what gemini-embedding-001 is asked to return (outputDimensionality).
"""

import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Index, text
from sqlmodel import Field, SQLModel


class Embedding(SQLModel, table=True):
    __tablename__ = "embeddings"
    __table_args__ = (
        # HNSW index for cosine similarity search (Phase 23). Partial: only summaries are ever
        # searched, and in an index over both kinds every other neighbour would be a full text
        # the query then throws away (migration 004, DECISIONS #142).
        Index(
            "ix_embeddings_summary_hnsw",
            "vector",
            postgresql_using="hnsw",
            postgresql_ops={"vector": "vector_cosine_ops"},
            postgresql_where=text("kind = 'summary'"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    itinerary_id: uuid.UUID = Field(foreign_key="itineraries.id", index=True, ondelete="CASCADE")

    embedding_model: str = Field(max_length=100)  # e.g. "gemini-embedding-001"
    # "summary" | "full_text"; NULL on a pending_retry row, which is no text's vector yet
    kind: str | None = Field(default=None, max_length=20)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    # pgvector column — dimension must match the chosen embedding model
    vector: list[float] | None = Field(
        default=None,
        sa_column=Column(Vector(1536), nullable=True),
    )

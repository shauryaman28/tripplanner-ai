"""Embedding table — pgvector storage for itinerary similarity (Phase 23).

Two rows per itinerary are written (Phase 14):
  1. full text embedding  — all detail
  2. structured summary   — destination + budget range + top-5 activities
     (better similarity signal for the search use-case)

The vector column uses pgvector's Vector(1536) type which maps to the
PostgreSQL 'vector' type added by the pgvector extension.
Dimension 1536 = OpenAI text-embedding-3-small output size.
"""

import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column
from sqlmodel import Field, SQLModel


class Embedding(SQLModel, table=True):
    __tablename__ = "embeddings"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    itinerary_id: uuid.UUID = Field(foreign_key="itineraries.id", index=True)

    embedding_model: str = Field(max_length=100)  # e.g. "text-embedding-3-small"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    # pgvector column — dimension must match the chosen embedding model
    vector: list[float] | None = Field(
        default=None,
        sa_column=Column(Vector(1536), nullable=True),
    )

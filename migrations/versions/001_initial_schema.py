"""Initial schema — users, trips, itineraries, agent_runs, embeddings

Revision ID: 001
Revises:
Create Date: 2026-07-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pgvector must be enabled before we create the vector column.
    # init.sql enables it at container creation time, but this makes
    # the migration idempotent when run against a fresh DB.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── users ─────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ── trips ─────────────────────────────────────────────────────────────
    op.create_table(
        "trips",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("destination", sa.String(200), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("budget", sa.Float(), nullable=False),
        sa.Column("group_size", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("interests", JSONB(), nullable=True),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="pending"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_trips_user_id", "trips", ["user_id"])

    # ── itineraries ───────────────────────────────────────────────────────
    op.create_table(
        "itineraries",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "trip_id",
            UUID(as_uuid=True),
            sa.ForeignKey("trips.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("structured_data", JSONB(), nullable=True),
        sa.Column("total_cost", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_itineraries_trip_id", "itineraries", ["trip_id"])

    # ── agent_runs ────────────────────────────────────────────────────────
    op.create_table(
        "agent_runs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "trip_id",
            UUID(as_uuid=True),
            sa.ForeignKey("trips.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_name", sa.String(100), nullable=False),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="pending"
        ),
        sa.Column("input", JSONB(), nullable=True),
        sa.Column("output", JSONB(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_agent_runs_trip_id", "agent_runs", ["trip_id"])

    # ── embeddings ────────────────────────────────────────────────────────
    op.create_table(
        "embeddings",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "itinerary_id",
            UUID(as_uuid=True),
            sa.ForeignKey("itineraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("embedding_model", sa.String(100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Add vector column using raw SQL — pgvector type is not a standard SA type
    op.execute("ALTER TABLE embeddings ADD COLUMN vector vector(1536)")
    # HNSW index for fast similarity search (Phase 23)
    op.execute(
        "CREATE INDEX ix_embeddings_vector_hnsw "
        "ON embeddings USING hnsw (vector vector_cosine_ops)"
    )
    op.create_index("ix_embeddings_itinerary_id", "embeddings", ["itinerary_id"])


def downgrade() -> None:
    op.drop_table("embeddings")
    op.drop_table("agent_runs")
    op.drop_table("itineraries")
    op.drop_table("trips")
    op.drop_table("users")

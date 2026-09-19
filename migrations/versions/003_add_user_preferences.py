"""Add user_preferences table — Phase 16 personalisation

Revision ID: 003
Revises: 002
Create Date: 2026-07-26
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        # One row per user → the FK doubles as the primary key.
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "dietary_restrictions",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "preferred_airlines",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("travel_style", sa.String(20), nullable=True),
        sa.Column("home_city", sa.String(100), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "travel_style IN ('budget', 'mid-range', 'luxury')",
            name="ck_user_preferences_travel_style",
        ),
    )


def downgrade() -> None:
    op.drop_table("user_preferences")

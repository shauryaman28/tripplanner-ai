"""Add turn column to agent_runs — Phase 15 multi-turn refinement

Revision ID: 002
Revises: 001
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Default 1 so every existing row is "first turn" — no data loss.
    op.add_column(
        "agent_runs",
        sa.Column("turn", sa.Integer(), nullable=False, server_default="1"),
    )
    # Index lets GET /trips/{id}/runs?turn=N filter efficiently.
    op.create_index("ix_agent_runs_turn", "agent_runs", ["trip_id", "turn"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_turn", table_name="agent_runs")
    op.drop_column("agent_runs", "turn")

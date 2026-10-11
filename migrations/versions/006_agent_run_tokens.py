"""What each run asked of the models — Phase 26 token and cost tracking

Revision ID: 006
Revises: 005
Create Date: 2026-10-11

`agent_runs.token_count` and `agent_runs.cost_usd`: the tokens in and out of
every model a run asked, and their list price in US dollars. NULL for every row
so far — what those runs used was never recorded and cannot be recovered — and
for every later row whose work asked no model.
"""

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("token_count", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("cost_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "cost_usd")
    op.drop_column("agent_runs", "token_count")

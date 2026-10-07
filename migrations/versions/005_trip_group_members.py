"""Who travels, and what each enjoys — Phase 25 group trips

Revision ID: 005
Revises: 004
Create Date: 2026-10-05

`trips.group_members` holds a group's travellers as the traveller entered
them: [{"name": "Asha", "interests": ["beach", "food"]}, …]. NULL for every
trip so far, and for any trip planned as one traveller — or as several who
were not told apart — from now on: such a trip is planned exactly as before.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trips", sa.Column("group_members", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("trips", "group_members")

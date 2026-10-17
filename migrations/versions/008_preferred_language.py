"""The language a traveller's plans are written in — Phase 36

Revision ID: 008
Revises: 007
Create Date: 2026-10-17

`user_preferences.preferred_language`: "en", "hi", "ta" or "bn" — English, Hindi, Tamil, Bengali.
NULL, as every existing row is left: none chosen, and each request's own language decides
(src/ai/language.py). Set, it is the language of every plan made for that traveller afterwards.
Downgrading drops the column: the choices made are lost, and plans follow the request again.
"""

import sqlalchemy as sa
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_preferences", sa.Column("preferred_language", sa.String(length=5), nullable=True))
    op.create_check_constraint(
        "ck_user_preferences_preferred_language",
        "user_preferences",
        "preferred_language IN ('en', 'hi', 'ta', 'bn')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_user_preferences_preferred_language", "user_preferences", type_="check")
    op.drop_column("user_preferences", "preferred_language")

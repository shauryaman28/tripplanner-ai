"""Notifications — Phase 40

Revision ID: 009
Revises: 008
Create Date: 2026-10-25

`user_preferences.notification_email` (on unless turned off) and `reminder_hours_before` (24, from 1 to 168):
what a traveller is sent, and when. `notifications`: one row per email a trip was due — which trip, which kind
("itinerary_ready", "trip_reminder"), what about (the first itinerary; the start date the reminder was for), and
what became of it (sent, logged, off, failed). Unique by (trip, kind, about): a trip is never sent the same email
twice, however many backends look for reminders at once. Downgrading drops both.
"""

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_preferences",
        sa.Column("notification_email", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "user_preferences",
        sa.Column("reminder_hours_before", sa.Integer(), nullable=False, server_default="24"),
    )
    op.create_check_constraint(
        "ck_user_preferences_reminder_hours", "user_preferences", "reminder_hours_before BETWEEN 1 AND 168"
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("trip_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("about", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trip_id"], ["trips.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trip_id", "kind", "about", name="uq_notifications_once"),
    )
    op.create_index(op.f("ix_notifications_trip_id"), "notifications", ["trip_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_notifications_trip_id"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_constraint("ck_user_preferences_reminder_hours", "user_preferences", type_="check")
    op.drop_column("user_preferences", "reminder_hours_before")
    op.drop_column("user_preferences", "notification_email")

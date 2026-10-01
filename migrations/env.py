"""Alembic environment configuration.

Run from project root:
    alembic upgrade head
    alembic downgrade -1
    alembic revision --autogenerate -m "description"

The DATABASE_URL from config is used automatically (driver swapped to psycopg2).
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, make_url, pool
from sqlmodel import SQLModel

# Add src/backend to path so app.* imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "backend"))

# Import all models — they self-register with SQLModel.metadata on import
from app.core.config import settings
from app.models import AgentRun, Embedding, Itinerary, Trip, User, UserPreferences  # noqa: F401

# ── Alembic config ────────────────────────────────────────────────────────

alembic_cfg = context.config
fileConfig(alembic_cfg.config_file_name, disable_existing_loggers=False)  # type: ignore[arg-type]

# Alembic runs synchronously — swap whatever driver the app uses for psycopg2.
sync_url = make_url(settings.DATABASE_URL).set(drivername="postgresql+psycopg2").render_as_string(hide_password=False)
alembic_cfg.set_main_option("sqlalchemy.url", sync_url.replace("%", "%%"))

target_metadata = SQLModel.metadata


# ── Migration runners ─────────────────────────────────────────────────────


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (generates SQL script)."""
    url = alembic_cfg.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""
    connectable = engine_from_config(
        alembic_cfg.get_section(alembic_cfg.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

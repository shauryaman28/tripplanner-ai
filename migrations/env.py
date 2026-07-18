"""Alembic environment configuration.

Run from project root:
    alembic upgrade head
    alembic downgrade -1
    alembic revision --autogenerate -m "description"

The DATABASE_URL from config is used automatically.
asyncpg scheme is swapped to psycopg2 for synchronous Alembic runs.
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

# Add src/backend to path so app.* imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "backend"))

# Import all models — they self-register with SQLModel.metadata on import
from app.models import AgentRun, Embedding, Itinerary, Trip, User  # noqa: F401
from app.core.config import settings

# ── Alembic config ────────────────────────────────────────────────────────

alembic_cfg = context.config
fileConfig(alembic_cfg.config_file_name)  # type: ignore[arg-type]

# Swap asyncpg → psycopg2 for synchronous Alembic runner
sync_url = (
    settings.DATABASE_URL
    .replace("postgresql+asyncpg://", "postgresql://")
    .replace("postgresql://", "postgresql+psycopg2://", 1)
    if "postgresql+asyncpg://" in settings.DATABASE_URL
    else settings.DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
)
alembic_cfg.set_main_option("sqlalchemy.url", sync_url)

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

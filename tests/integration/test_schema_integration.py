"""Phase 4 — the Alembic history and the SQLModel models must describe the same schema.

RUN_INTEGRATION=1 pytest tests/integration/test_schema_integration.py -v
"""

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)


def test_models_match_migrations():
    """`alembic check` raises if autogenerate would emit anything — i.e. a model changed without a migration."""
    command.check(Config("alembic.ini"))


@pytest.mark.asyncio
async def test_schema_has_all_tables_and_the_pgvector_column(db_session):
    tables = (await db_session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars()
    assert {"users", "trips", "itineraries", "agent_runs", "embeddings", "user_preferences"} <= set(tables)

    vector_type = await db_session.scalar(
        text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'embeddings'::regclass AND attname = 'vector'"
        )
    )
    assert vector_type == "vector(1536)"

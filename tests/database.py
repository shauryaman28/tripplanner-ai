"""A dedicated, migrated Postgres database for anything that must not touch dev data.

`<db>_test` for the integration tests, `<db>_e2e` for the Playwright stub backend.
Both are built with the real Alembic migrations, so they have the production
schema — cascades, HNSW index and all.
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text

from app.core.config import settings


def use_database(suffix: str) -> None:
    """Point `settings.DATABASE_URL` at `<db><suffix>`.

    Must run before `app.db.session` is imported — it builds its engine at import time.
    """
    url = make_url(settings.DATABASE_URL)
    if not url.database.endswith(suffix):
        settings.DATABASE_URL = url.set(database=f"{url.database}{suffix}").render_as_string(hide_password=False)


def create_and_migrate() -> None:
    """Create the database if it is missing and bring it to head."""
    url = make_url(settings.DATABASE_URL).set(drivername="postgresql+psycopg2")

    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}):
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()

    command.upgrade(Config("alembic.ini"), "head")


def empty_all_tables() -> None:
    """Delete every row. Every other table hangs off `users`, so one cascade does it."""
    engine = create_engine(make_url(settings.DATABASE_URL).set(drivername="postgresql+psycopg2"))
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users CASCADE"))
    engine.dispose()

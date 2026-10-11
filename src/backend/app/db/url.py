"""One DATABASE_URL, as each of the two drivers needs it (Phase 27).

A hosted Postgres hands out one connection string, written for libpq:

    postgresql://user:password@ep-cool-name-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require

Two drivers read it here. psycopg2 (Alembic, the tests' helpers) *is* libpq and
takes it as it stands. asyncpg (the app) is not, and SQLAlchemy hands it every
query parameter as a keyword argument: `sslmode` and `channel_binding` are not
ones it has, and the app would not start — `connect() got an unexpected keyword
argument 'sslmode'`. So the parameters are translated for it here, in one place.

The address a hosted Postgres recommends is a connection pooler in transaction
mode (PgBouncer): a server connection is lent for one transaction and then
given to somebody else. A prepared statement belongs to a server connection, so
one kept for later is, later, on a connection that has never heard of it. The
app therefore keeps none: no statement cache in asyncpg, none in SQLAlchemy,
and a name of its own for each statement it does prepare. That costs a parse
per query and works on every address — pooled or direct, with or without a
pooler that tracks prepared statements itself.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import URL, make_url

logger = logging.getLogger(__name__)

# libpq's sslmode values are asyncpg's `ssl` values, word for word.
SSL_MODES = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})

# Parameters asyncpg has no use for and connects correctly without.
#   channel_binding  asyncpg authenticates with SCRAM-SHA-256 and has no channel binding to switch on;
#                    the connection is still encrypted, by `sslmode`.
_DROPPED = frozenset({"channel_binding", "gssencmode", "sslnegotiation"})


def normalised(database_url: str) -> URL:
    """The URL without a driver: `postgres://…` (as some platforms write it) is `postgresql://…`."""
    url = make_url(database_url.strip())
    if url.get_backend_name() not in ("postgresql", "postgres"):
        raise ValueError(f"DATABASE_URL must be a postgresql:// URL, not {url.get_backend_name()}://")
    return url.set(drivername="postgresql")


def sync_url(database_url: str) -> str:
    """For psycopg2: libpq's own URL, every parameter as it was given."""
    return normalised(database_url).set(drivername="postgresql+psycopg2").render_as_string(hide_password=False)


def _unique_statement_name() -> str:
    return f"__asyncpg_{uuid.uuid4().hex}__"


def async_engine_arguments(database_url: str) -> tuple[URL, dict[str, Any]]:
    """(url, connect_args) for `create_async_engine` — libpq's parameters as asyncpg's.

    sslmode=require        → ssl="require"      (also verify-ca, verify-full, disable, …)
    connect_timeout=10     → timeout=10.0
    options=…, application_name=…  → server_settings, sent as the connection starts
    channel_binding=…      → left out (see _DROPPED)
    anything else          → left out, and named in the log — never passed on to fail the connection
    """
    url = normalised(database_url)
    query = {key: value if isinstance(value, str) else value[-1] for key, value in url.query.items()}
    connect_args: dict[str, Any] = {}
    server_settings: dict[str, str] = {}

    mode = query.pop("ssl", None) or query.pop("sslmode", None)
    query.pop("sslmode", None)
    if mode:
        if mode not in SSL_MODES:
            raise ValueError(f"DATABASE_URL: sslmode must be one of {sorted(SSL_MODES)}, not {mode!r}")
        connect_args["ssl"] = mode
    if "connect_timeout" in query:
        connect_args["timeout"] = float(query.pop("connect_timeout"))
    for setting in ("options", "application_name"):
        if setting in query:
            server_settings[setting] = query.pop(setting)
    if server_settings:
        connect_args["server_settings"] = server_settings

    ignored = sorted(key for key in query if key not in _DROPPED)
    if ignored:
        logger.warning("DATABASE_URL: %s left out — asyncpg has no such parameter", ", ".join(ignored))

    # Nothing prepared is kept from one transaction to the next: see the module's docstring.
    connect_args["statement_cache_size"] = 0
    connect_args["prepared_statement_cache_size"] = 0
    connect_args["prepared_statement_name_func"] = _unique_statement_name
    return url.set(drivername="postgresql+asyncpg", query={}), connect_args


def where(database_url: str) -> str:
    """The database as it may be logged: host, port and name — no user, no password."""
    try:
        url = make_url(database_url.strip())
    except Exception:
        return "an unreadable URL"
    port = f":{url.port}" if url.port else ""
    return f"{url.host or '?'}{port}/{url.database or ''}"

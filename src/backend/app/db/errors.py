"""Telling "the database cannot be reached" from every other way a statement can fail (Phase 32).

Until this phase nothing in the app made the distinction. Postgres was killed under a running
stack (docs/chaos-data-report.md) and what came of it was whatever each caller did with an
exception it had never met: a traveller's page was shown the driver's own account of it — the SQL,
the parameters, the class name — and a request was a 500, "something went wrong on our side".

A database that is away is not a fault in a statement. It passes, and the right things to do about
it are the same everywhere: say so in words, and try again later.
"""

from __future__ import annotations

from collections.abc import Iterator

import asyncpg
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import InterfaceError as DriverInterfaceError
from sqlalchemy.exc import TimeoutError as PoolTimeout

# What the driver raises when there is nobody at the other end — the server gone, the pooler having
# closed the connection, a server that is starting up or has no room — or nobody answering in time.
_NOBODY_THERE = (
    asyncpg.PostgresConnectionError,  # SQLSTATE class 08: "connection was closed in the middle of operation", …
    asyncpg.InterfaceError,  # the client's own: "connection is closed"
    asyncpg.CannotConnectNowError,  # "the database system is starting up"
    asyncpg.TooManyConnectionsError,
    ConnectionError,  # refused, reset, a broken pipe: there was no connection to make or keep
    TimeoutError,  # no answer within the time a statement is given (db/session.py)
)


def _behind(exc: BaseException) -> Iterator[BaseException]:
    """An error and what it was made from: the driver's error under SQLAlchemy's, and so on down."""
    seen: set[int] = set()
    waiting = [exc]
    while waiting and len(seen) < 12:
        error = waiting.pop()
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        yield error
        waiting += [getattr(error, "orig", None), error.__cause__, error.__context__]


def database_is_away(exc: BaseException) -> bool:
    """Whether an error means the database could not be reached — not that it refused what it was asked."""
    if isinstance(exc, (OperationalError, DriverInterfaceError, PoolTimeout)):
        return True
    if isinstance(exc, DBAPIError):
        return exc.connection_invalidated or any(isinstance(error, _NOBODY_THERE) for error in _behind(exc))
    return isinstance(exc, (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, ConnectionError))

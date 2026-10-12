"""Which trip, and whose, the work under way is for (Phase 29).

Two context variables. A request sets them as it comes in (the middleware in
app/main.py reads the trip from the address and the user from the login token);
every task the request starts — the planning run, the cache warming — begins
with a copy, so the run that outlives its request still knows whose it is. The
tool server is another process: the caller sends both with each call, and the
tool server sets them for as long as it works on it.

Two readers:

- logs.py puts them on every log line;
- traces.py puts them on every span, as `trip.id` and `user.id`.

So one trip id finds its log lines and its traces, whichever process wrote them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry import trace

TRIP_ATTRIBUTE = "trip.id"
USER_ATTRIBUTE = "user.id"

_trip_id: ContextVar[str | None] = ContextVar("telemetry_trip_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("telemetry_user_id", default=None)


def trip_id() -> str | None:
    return _trip_id.get()


def user_id() -> str | None:
    return _user_id.get()


def attributes() -> dict[str, str]:
    """What is known, as span attributes — nothing for what is not."""
    known = {TRIP_ATTRIBUTE: _trip_id.get(), USER_ATTRIBUTE: _user_id.get()}
    return {name: value for name, value in known.items() if value}


def bind(trip_id: Any = None, user_id: Any = None) -> None:
    """From here on — in this task, and in every task it starts — the work is for this trip, this user.

    Only what is given changes. The span under way is told as well: it began before anyone knew.
    """
    if trip_id is not None:
        _trip_id.set(str(trip_id))
    if user_id is not None:
        _user_id.set(str(user_id))
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attributes(attributes())


@contextmanager
def scope(trip_id: Any = None, user_id: Any = None) -> Iterator[None]:
    """The same for the length of a block; afterwards both are what they were before it."""
    before = _trip_id.set(str(trip_id) if trip_id else None), _user_id.set(str(user_id) if user_id else None)
    try:
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attributes(attributes())
        yield
    finally:
        _trip_id.reset(before[0])
        _user_id.reset(before[1])

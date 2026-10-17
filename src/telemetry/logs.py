"""Every log line a JSON object that says whose it is (Phase 29).

    {"timestamp": "2026-10-11T15:04:05.123+00:00", "level": "info", "logger": "src.ai.orchestrator.warming",
     "message": "[CACHE WARM] flights: DEL → Goa on 2026-12-10, back 2026-12-15 — 3 result(s) ready in 1.2 s",
     "service": "tripplanner-backend", "trip_id": "3f2a…", "user_id": "9c1d…",
     "trace_id": "5b8e…", "span_id": "a1c4…"}

`LOG_FORMAT=json` switches it on (the default where APP_ENV is production); `text` leaves the
console as it was, for a person to read.

What makes a line findable is not written by whoever logs it. A filter on the one handler adds the
trip and the user from two context variables (context.py) and the trace from the span under way —
to every record, whoever made it: this app, the web server, a library. Nothing that logs has to
know. `trip_id` and `user_id` are on every line, `null` where the work is nobody's (startup, a
health check): a field that is sometimes missing is harder to search than one that is sometimes empty.

Both processes log this way, each under its own `service`: the API server to standard output, the
tool server to standard error — its standard output is the protocol it speaks.

Two things are deliberately not logged at INFO. A health check that was answered: a platform asks
every few seconds, and those lines would be most of the log. And libraries: at INFO an HTTP client
logs every address it calls, and some providers are sent their key in the address.

A line that is kept and searched is read by more people, and for longer, than one that scrolls past
on a console. So a JSON line's message and its traceback go through the same scrubber as the error
reports (app/core/error_reporting.py) before they are written: a provider's key in the address of
a request that failed is taken out here too. And an access line is written without the address the
request came from — whose it was is `user_id`.

Python's own warnings (a library saying a model's name is about to change) are written by another
mechanism, as bare text; in JSON they are taken into the log, so that they are lines like the rest.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TextIO

from pythonjsonlogger.core import RESERVED_ATTRS
from pythonjsonlogger.json import JsonFormatter

from src.telemetry import context, traces

SQL_ECHO = "sqlalchemy.engine.Engine"
# Loggers that arrive with handlers of their own. In JSON they hand their lines to the one handler instead.
TAKEN_OVER = ("uvicorn", "uvicorn.error", "uvicorn.access", SQL_ECHO)
# Whose INFO lines are wanted; everybody else is heard from WARNING up.
INFO_FROM = ("app", "src", "uvicorn", "__main__")
# How the web server begins a line of its access log: with the address the request came from.
_FROM_WHOM = re.compile(r"^\S+:\d+ - ")


def chosen(log_format: str, app_env: str) -> str:
    """The format in force: what LOG_FORMAT says, else JSON in production and text anywhere else."""
    asked = (log_format or "").strip().lower()
    if asked in ("json", "text"):
        return asked
    return "json" if str(app_env).lower() == "production" else "text"


class ContextFilter(logging.Filter):
    """Adds to every record: the service, the trip, the user, the trace — and an access line's parts."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self.service
        record.trip_id = context.trip_id()
        record.user_id = context.user_id()
        record.trace_id, record.span_id = traces.current_ids()
        asked = _access(record)
        if asked is not None:
            record.method, record.path, record.status = asked
        return True


def _access(record: logging.LogRecord) -> tuple[str, str, int] | None:
    """(method, path, status) of a line of the web server's access log; None for any other line.

    The path without its query: an event stream's address carries the login token there.
    """
    args = record.args
    if record.name != "uvicorn.access" or not isinstance(args, tuple) or len(args) != 5:
        return None
    try:
        return str(args[1]), str(args[2]).split("?")[0], int(args[4])
    except (TypeError, ValueError):
        return None


class QuietProbes(logging.Filter):
    """The access log without the health checks that were answered. One that failed is still a line."""

    def filter(self, record: logging.LogRecord) -> bool:
        asked = _access(record)
        return asked is None or asked[1] not in traces.PROBES or asked[2] >= 400


class WithoutSqlEcho(logging.Filter):
    """In development the database engine echoes every statement as a log line — nine lines in ten of a plan's.

    Where lines are stored and searched, a statement is a span of the trace instead. (A filter on
    the handler, not a level on the logger: the engine's echo writes past its logger's level.)
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.name == SQL_ECHO and record.levelno < logging.WARNING)


class JsonLines(JsonFormatter):
    """One JSON object per line: timestamp (UTC, to the millisecond), level, logger, message, then what the
    filter added and whatever the caller passed as `extra`. `scrub`: what the texts are put through first."""

    SCRUBBED = ("message", "exc_info", "stack_info")

    def __init__(self, scrub: Callable[[str], str] | None = None) -> None:
        super().__init__(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
            reserved_attrs=[*RESERVED_ATTRS, "color_message"],  # the web server's own copy of a line, with colour codes
            json_ensure_ascii=False,  # "—" and "₹" as themselves: a line is read by people too
        )
        self._scrub = scrub

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds")

    def process_log_record(self, log_data: dict[str, Any]) -> dict[str, Any]:
        log_data["level"] = str(log_data.get("level", "")).lower()
        if log_data.get("logger") == "uvicorn.access" and isinstance(log_data.get("message"), str):
            log_data["message"] = _FROM_WHOM.sub("", log_data["message"])
        if self._scrub is not None:
            for field in self.SCRUBBED:
                if isinstance(log_data.get(field), str):
                    log_data[field] = self._scrub(log_data[field])
        return log_data


class LogRedactor(logging.Filter):
    """Phase 35 — every log line put through the scrubber, whatever writes it and in whatever format.

    Until Phase 35 only a JSON line was scrubbed (in its formatter): a development console, a text
    log file and every handler a library brings wrote what they were given. This works on the
    record itself — its message with the arguments put in, its traceback and stack — so every
    handler after it sees the same clean text. `redact_every_record` puts it in front of all of
    them at once; it is also an ordinary filter, for a handler of one's own.
    """

    def __init__(self, scrub: Callable[[str], str]) -> None:
        super().__init__()
        self.scrub = scrub

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a message whose arguments do not fit it: logging says so itself, later
            return True
        cleaned = self.scrub(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = self.scrub(record.stack_info)
        return True


_redactor: LogRedactor | None = None


def redact_every_record(scrub: Callable[[str], str]) -> LogRedactor:
    """Every record this process makes from now on goes through a LogRedactor as it is made — before any
    handler, any format, any library's own handler sees it. Safe to call again: the scrubber is replaced,
    not a second redactor added."""
    global _redactor
    if _redactor is not None:
        _redactor.scrub = scrub
        return _redactor
    _redactor = redactor = LogRedactor(scrub)
    make = logging.getLogRecordFactory()

    def redacted(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = make(*args, **kwargs)
        redactor.filter(record)
        return record

    logging.setLogRecordFactory(redacted)
    return redactor


def configure(
    log_format: str, service: str, stream: TextIO | None = None, scrub: Callable[[str], str] | None = None
) -> bool:
    """`json`: from now on every line this process logs is one JSON object on `stream`. Returns whether it is.

    Anything else changes nothing — the console stays as whoever started the process set it up.
    Safe to call again: the handler is replaced, not added to.
    """
    if log_format != "json":
        return False
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonLines(scrub))
    handler.addFilter(ContextFilter(service))
    handler.addFilter(WithoutSqlEcho())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    for name in INFO_FROM:
        logging.getLogger(name).setLevel(logging.INFO)
    for name in TAKEN_OVER:
        theirs = logging.getLogger(name)
        theirs.handlers.clear()
        theirs.propagate = True
    logging.captureWarnings(True)  # Python's warnings: lines of the log (logger "py.warnings"), not bare text

    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, QuietProbes) for existing in access.filters):
        access.addFilter(QuietProbes())
    return True

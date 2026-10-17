"""Errors reported to Sentry — and what is taken out of a report before it leaves (Phase 27).

On with a `SENTRY_DSN`; without one, nothing here does anything. `APP_ENV=test`
keeps it off whatever `.env` says: the test stacks raise on purpose all day.

What Sentry gets, by itself, once it is on:

- every exception no handler caught — a request that ended in a 500;
- every `logger.error` / `logger.exception` — which is how a planning run that
  crashed in the background gets there (routes/trips.py logs it, tagged with
  its trip);
- one request in ten as a performance trace (`SENTRY_TRACES_SAMPLE_RATE`).

What it must not get. A report is built from whatever was in reach when the
error happened, and in this app that includes:

- the login token, in the URL of every event stream (`/trips/<id>/stream?token=…`
  — a browser's EventSource cannot send a header);
- the providers' keys, in the URLs of the tools' own requests (`…?apikey=…`,
  `…?appid=…`), which the SDK notes down as breadcrumbs;
- the database's and the Redis's passwords, inside their URLs — and every
  secret at once in the repr of the settings object, a local variable of the
  code that starts the app;
- passwords in the body of a login, e-mail addresses in a database error.

So the SDK is told to send no local variables and no request bodies, and
`Scrubber` goes through everything that is left — every string of every event,
breadcrumb and trace — and takes out: each secret this process holds, by value;
whatever a URL carries under a name like `token` or `key`; credentials inside a
URL; bearer tokens and JWTs; e-mail addresses. By value is the part that does
not depend on guessing where a secret might turn up.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

import sentry_sdk
from sqlalchemy import make_url

logger = logging.getLogger(__name__)

FILTERED = "[Filtered]"
FLUSH_SECONDS = 2.0  # how long shutdown waits for the last reports to be sent
MIN_SECRET_LENGTH = 6  # shorter values are not taken out by value: "postgres" would vanish from every sentence

# A setting whose name says it is a secret. The DSN is not one: it only allows sending.
# (…OTLP_HEADERS, Phase 29: where a hosted tracing backend's key goes.)
_SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|OTLP_HEADERS", re.I)
_URL_SETTINGS = ("DATABASE_URL", "DATABASE_URL_UNPOOLED", "REDIS_URL")

# name=value in a query string, a form body, a header dump — when the name says the value is a secret
_SECRET_PARAMETER = re.compile(
    r"(?i)(\b(?:token|access_token|refresh_token|id_token|key|api_?key|apikey|appid|secret|client_secret|"
    r"password|passwd|pwd|signature|sig)=)[^&\s#\"'<>]+"
)
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@\"']+:[^/\s@\"']+@")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/\-]+=*")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")
# Phase 35: what a provider's key looks like, for a key this process was never given (an old one, another
# deployment's): Google's AIza… and its newer AQ.…, Groq's gsk_, Duffel's, LangSmith's lsv2_, LiteAPI's,
# Anthropic's sk-ant-, OpenAI's sk-. Bare hex keys (OpenTripMap's, OpenWeather's) have no shape a trace id
# or a cache digest does not share: those are taken out by value, and by name in a query string.
_KEY_SHAPES = re.compile(
    r"\bAIza[0-9A-Za-z_\-]{35}\b"
    r"|\bAQ\.[0-9A-Za-z_\-]{40,}"
    r"|\bgsk_[0-9A-Za-z]{40,}"
    r"|\bduffel_(?:test|live)_[0-9A-Za-z_\-]{20,}"
    r"|\blsv2_(?:pt|sk)_[0-9a-f]{20,}_[0-9a-f]{6,}"
    r"|\b(?:sand|prod)_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\bsk-ant-[0-9A-Za-z_\-]{20,}"
    r"|\bsk-(?:proj-)?[0-9A-Za-z_\-]{32,}"
)
# An address ends in letters: "tripplanner-ai@0.27.0" is a release, not somebody's e-mail.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}\b")
# A dict key whose value is a secret whatever it looks like (headers, extras, cookies) — "x-ops-token",
# "access_token", not "token_count".
_SECRET_KEY = re.compile(r"(?i)authorization|cookie|secret|password|passwd|api[-_]?key|(?:^|[-_])token$")
# The server's access log is kept as breadcrumbs, and each line begins with who asked: an address.
_CLIENT_ADDRESS = re.compile(r"^\S+:\d+ - ")


def secrets_of(*settings_objects: Any) -> list[str]:
    """Every secret value these settings hold: by its setting's name, and the passwords inside their URLs."""
    found: set[str] = set()
    for settings in settings_objects:
        for name in type(settings).model_fields:
            value = getattr(settings, name, None)
            if not isinstance(value, str) or not value:
                continue
            if _SECRET_NAME.search(name):
                found.add(value)
            if name in _URL_SETTINGS:
                try:
                    password = make_url(value).password
                except Exception:
                    password = None
                if password:
                    found.update({str(password), quote(str(password), safe="")})
    return sorted((secret for secret in found if len(secret) >= MIN_SECRET_LENGTH), key=len, reverse=True)


class Scrubber:
    """Takes what must not leave out of an event, a breadcrumb or a trace — in place, and returns it."""

    def __init__(self, secrets: list[str]) -> None:
        self._secrets = [secret for secret in secrets if len(secret) >= MIN_SECRET_LENGTH]

    def text(self, value: str) -> str:
        for secret in self._secrets:
            if secret in value:
                value = value.replace(secret, FILTERED)
        value = _URL_CREDENTIALS.sub(rf"\1{FILTERED}@", value)
        value = _SECRET_PARAMETER.sub(rf"\1{FILTERED}", value)
        value = _BEARER.sub(rf"\1{FILTERED}", value)
        value = _JWT.sub(FILTERED, value)
        value = _KEY_SHAPES.sub(FILTERED, value)
        return _EMAIL.sub(FILTERED, value)

    def clean(self, value: Any, key: str = "") -> Any:
        if isinstance(value, str):
            return FILTERED if key and _SECRET_KEY.search(key) and value else self.text(value)
        if isinstance(value, dict):
            for name in list(value):
                value[name] = self.clean(value[name], str(name))
            return value
        if isinstance(value, list):
            # a list of [name, value] pairs is how headers and query strings are sometimes kept
            if len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], str) and key != "values":
                return [value[0], self.clean(value[1], value[0])]
            return [self.clean(item, key) for item in value]
        if isinstance(value, tuple):
            return tuple(self.clean(item, key) for item in value)
        return value

    # The three hooks the SDK calls. A scrubber that fails must not let the report through as it was.
    def event(self, event: dict, _hint: Any = None) -> dict | None:
        try:
            return self.clean(event)
        except Exception:
            logger.warning("An error report could not be scrubbed and was not sent", exc_info=True)
            return None

    def breadcrumb(self, crumb: dict, _hint: Any = None) -> dict | None:
        try:
            if crumb.get("category") == "uvicorn.access" and isinstance(crumb.get("message"), str):
                crumb["message"] = _CLIENT_ADDRESS.sub("", crumb["message"])
            return self.clean(crumb)
        except Exception:
            return None


class AccessLogFilter(logging.Filter):
    """The server's access log without the login token in it.

    An event stream is opened as `GET /trips/<id>/stream?token=<the login token>`
    — a browser's EventSource has no other way to say who it is — and the server
    logs every request's address in full. A log is read by more people, and kept
    for longer, than a token should be.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "=" in args[2]:
            record.args = (*args[:2], _SECRET_PARAMETER.sub(rf"\1{FILTERED}", args[2]), *args[3:])
        return True


def quiet_access_log() -> None:
    """Take the tokens out of the access log. Safe to call more than once."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, AccessLogFilter) for existing in access.filters):
        access.addFilter(AccessLogFilter())


def release() -> str:
    """What is running: the commit a platform built (Railway says), or the app's version."""
    from app import __version__
    from app.core.config import settings

    commit = settings.commit
    return f"tripplanner-ai@{__version__}" + (f"+{commit[:12]}" if commit else "")


def enabled() -> bool:
    return sentry_sdk.get_client().is_active()


def configure(settings: Any, *more_settings: Any, transport: Any = None) -> bool:
    """Switch error reporting on if the settings give a DSN. Returns whether it is on.

    `more_settings`: other settings objects whose secrets must be scrubbed too (the tool server's).
    `transport`: where reports are sent instead of to the DSN — the tests hand in one that keeps them.
    """
    if not settings.SENTRY_DSN:
        logger.info("[SENTRY] error reporting is off (SENTRY_DSN is empty)")
        return False
    if settings.APP_ENV == "test":
        logger.info("[SENTRY] error reporting is off (APP_ENV is test)")
        return False

    scrubber = Scrubber(secrets_of(settings, *more_settings))
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        environment=settings.APP_ENV,
        release=release(),
        # What a report may carry — see the module's docstring.
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        before_send=scrubber.event,
        before_send_transaction=scrubber.event,
        before_breadcrumb=scrubber.breadcrumb,
        **({"transport": transport} if transport is not None else {}),
    )
    logger.info(
        "[SENTRY] error reporting is on — environment %r, release %r, %.0f%% of requests traced",
        settings.APP_ENV,
        release(),
        settings.SENTRY_TRACES_SAMPLE_RATE * 100,
    )
    return True


@contextmanager
def run_scope(trip_id: Any, turn: int) -> Iterator[None]:
    """Whatever is reported inside is about this trip's run: tagged `trip_id` and `turn`.

    A scope of its own — a run is a background task started by a request, and
    would otherwise write its tags onto that request's scope. With reporting
    off this is nothing at all.
    """
    with sentry_sdk.isolation_scope() as scope:
        scope.set_tag("trip_id", str(trip_id))
        scope.set_tag("turn", str(turn))
        yield


def shutdown() -> None:
    """Send what is still waiting — for a moment at most."""
    if enabled():
        sentry_sdk.flush(timeout=FLUSH_SECONDS)


def switch_off() -> None:
    """Stop reporting: nothing is sent from here on. For the tests, which switch it on one at a time."""
    client = sentry_sdk.get_client()
    if client.is_active():
        client.close(timeout=0)
    sentry_sdk.get_global_scope().set_client(None)

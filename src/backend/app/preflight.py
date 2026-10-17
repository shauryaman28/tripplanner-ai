"""What a deployment must have before it starts — said at once, by name (Phase 27).

    python -m app.preflight                              # the environment the app is about to start in
    python -m app.preflight --env-file .env.production   # a file of values, before they are given to a platform
    python -m app.preflight --env-file .env.production --production

A first deploy fails one missing variable at a time: the container starts, one
thing is wrong, it stops; set it, deploy again, the next thing is wrong. This
reads the settings the way the app will and lists everything in one go:

  cannot start    with `APP_ENV=production`, the app refuses to start on these:
                  a signing secret anyone can read in the repository, a database
                  or a Redis that is this machine's own, a Redis URL that is a
                  REST API, a model key without which no plan can be written.
  will degrade    it starts, and part of it does not work: a provider's key that
                  is missing, errors that nobody is told about.

Outside production nothing here stops anything: a laptop is allowed its defaults.
No value is ever printed — a variable is named, and what is wrong with it.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

DEFAULT_JWT_SECRET = "change-me-before-production"
MIN_SECRET_LENGTH = 32
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal", ""})


@dataclass
class Report:
    production: bool
    errors: list[str] = field(default_factory=list)  # in production: the app does not start
    warnings: list[str] = field(default_factory=list)  # it starts, and something does not work

    @property
    def ok(self) -> bool:
        return not (self.production and self.errors)

    def text(self) -> str:
        lines = []
        if self.errors:
            lines.append("Cannot start in production:" if self.production else "Would not start in production:")
            lines += [f"  ✗ {problem}" for problem in self.errors]
        if self.warnings:
            lines.append("Starts, and will not fully work:")
            lines += [f"  ! {problem}" for problem in self.warnings]
        return "\n".join(lines) or "Every setting a deployment needs is there."


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _scheme(url: str) -> str:
    return url.split("://", 1)[0].lower() if "://" in url else ""


def check(settings, providers=None) -> Report:
    """Read the settings as a deployment would be judged. `providers`: the tool server's own settings."""
    report = Report(production=str(settings.APP_ENV).lower() == "production")
    wrong, weak = report.errors.append, report.warnings.append

    # ── what the app cannot run without ──
    if settings.JWT_SECRET == DEFAULT_JWT_SECRET:
        wrong("JWT_SECRET is the default from the repository: anyone could sign a login. Set: openssl rand -hex 32")
    elif len(settings.JWT_SECRET) < MIN_SECRET_LENGTH:
        wrong(f"JWT_SECRET is shorter than {MIN_SECRET_LENGTH} characters. Set: openssl rand -hex 32")

    if _scheme(settings.DATABASE_URL) not in ("postgresql", "postgres"):
        wrong("DATABASE_URL is not a postgresql:// URL")
    elif _host(settings.DATABASE_URL) in LOCAL_HOSTS:
        wrong("DATABASE_URL points at this machine (the development default) — set the hosted database's URL")

    redis_scheme, redis_host = _scheme(settings.REDIS_URL), _host(settings.REDIS_URL)
    if redis_scheme in ("http", "https"):
        wrong(
            "REDIS_URL is an https:// address — that is a REST API, and a REST API cannot subscribe: the live "
            "progress of a plan is a subscription. Use the rediss:// URL (TCP) of the same database"
        )
    elif redis_scheme not in ("redis", "rediss"):
        wrong("REDIS_URL is not a redis:// or rediss:// URL")
    elif redis_host in LOCAL_HOSTS:
        wrong("REDIS_URL points at this machine (the development default) — set the hosted Redis's URL")
    elif redis_scheme == "redis" and redis_host.endswith("upstash.io"):
        wrong("REDIS_URL is redis:// for an Upstash database — Upstash only answers over TLS: write rediss://")

    origins = [origin for origin in settings.cors_origins if _host(origin) not in LOCAL_HOSTS]
    if not origins and not settings.CORS_ORIGIN_REGEX:
        wrong(
            "CORS_ORIGINS names no deployed frontend: the page opens its event stream straight from the browser, "
            "and the browser will refuse it. Set the frontend's origin, e.g. https://your-app.vercel.app"
        )
    for origin in settings.cors_origins:
        if origin.endswith("/") or _scheme(origin) not in ("http", "https"):
            wrong(f"CORS_ORIGINS: {origin!r} is not an origin — write scheme://host with no path and no trailing slash")

    if not settings.GROQ_API_KEY:
        wrong("GROQ_API_KEY is empty: the itinerary is written by a Groq model — no plan could be made")

    # Phase 29 — where traces go, when they go anywhere
    traces_to = (getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "") or "").strip()
    if traces_to and _scheme(traces_to) not in ("http", "https"):
        wrong(
            "OTEL_EXPORTER_OTLP_ENDPOINT is not an http:// or https:// URL: traces are sent as OTLP over HTTP "
            "(a collector's port 4318) — a bare host, or a gRPC address, cannot be sent to"
        )
    if not 0.0 <= float(getattr(settings, "OTEL_TRACES_SAMPLE_RATIO", 1.0)) <= 1.0:
        wrong("OTEL_TRACES_SAMPLE_RATIO is not between 0 and 1: it is the share of requests that are traced")

    # ── what it runs without, worse ──
    if not settings.GOOGLE_API_KEY:
        weak(
            "GOOGLE_API_KEY is empty: requests are read by the fallback model, and no itinerary is embedded — "
            "similar trips and search find nothing"
        )
    if providers is not None:
        for name, what in (
            ("DUFFEL_ACCESS_TOKEN", "no flights are searched: plans are made without them"),
            ("LITEAPI_API_KEY", "no hotels are searched: plans are made without one"),
            ("OPENTRIPMAP_API_KEY", "no attractions are searched: there is nothing to plan the days from"),
        ):
            if not getattr(providers, name, ""):
                weak(f"{name} is empty: {what}")
    if not settings.SENTRY_DSN:
        weak("SENTRY_DSN is empty: errors are logged and reported to nobody")
    # Phase 35: what keeps a password from being guessed, and a stolen token from being good for long
    if getattr(settings, "LOGIN_ATTEMPTS_PER_MINUTE", 5) <= 0:
        weak(
            "LOGIN_ATTEMPTS_PER_MINUTE is 0: the limit on signing in is off — a password can be guessed as fast as bcrypt allows"
        )
    if getattr(settings, "REGISTRATIONS_PER_MINUTE", 5) <= 0:
        weak("REGISTRATIONS_PER_MINUTE is 0: accounts can be made without any limit")
    if getattr(settings, "JWT_EXPIRE_MINUTES", 60) > 60:
        weak(
            f"JWT_EXPIRE_MINUTES is {settings.JWT_EXPIRE_MINUTES}: a stolen access token is good for that long — "
            "staying signed in is the refresh token's job (60 is the default)"
        )
    if not settings.OPS_TOKEN:
        weak(
            "OPS_TOKEN is empty: GET /metrics answers nobody, so Prometheus cannot read this deployment"
            " — and GET /admin/health-deep answers nobody either, so its dependencies cannot be asked"  # Phase 34
            + (
                " — and GET /admin/sentry-test is switched off, so error reporting cannot be tried out"
                if settings.SENTRY_DSN
                else ""
            )
        )
    if settings.OPS_TOKEN and len(settings.OPS_TOKEN) < MIN_SECRET_LENGTH:
        weak(f"OPS_TOKEN is shorter than {MIN_SECRET_LENGTH} characters")
    if settings.LANGCHAIN_TRACING_V2 and not settings.LANGCHAIN_API_KEY:
        weak("LANGCHAIN_TRACING_V2 is true and LANGCHAIN_API_KEY is empty: no trace is uploaded")
    if not traces_to:
        weak(
            "OTEL_EXPORTER_OTLP_ENDPOINT is empty: no request is traced — a slow plan cannot be followed through the system"
        )
    elif traces_to.rstrip("/").endswith(":4317"):
        weak("OTEL_EXPORTER_OTLP_ENDPOINT ends in :4317, a collector's gRPC port: OTLP over HTTP is usually :4318")
    if str(getattr(settings, "LOG_FORMAT", "") or "").strip().lower() == "text":
        weak("LOG_FORMAT is text: the log lines cannot be searched by trip_id — leave it empty, or json")
    return report


class NotReady(RuntimeError):
    """The settings are not fit to serve from. The message lists every reason."""


def enforce(settings, providers=None) -> Report:
    """Raise NotReady when a production deployment must not start; return the report otherwise."""
    report = check(settings, providers)
    if not report.ok:
        raise NotReady(report.text())
    return report


def _only_from(model, env_file: str):
    """The settings a file holds, and nothing else's: what this shell happens to have exported is not
    what the platform will be given. (Settings read the environment before any file, so for the
    moment it takes to read the file, the environment's values for the same names are put aside.)"""
    names = {name.upper() for name in model.model_fields}
    aside = {name: os.environ.pop(name) for name in list(os.environ) if name.upper() in names}
    try:
        return model(_env_file=env_file)
    finally:
        os.environ.update(aside)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Say what a deployment's settings are missing, before it starts.")
    parser.add_argument("--env-file", help="read the settings from this file instead of the environment and .env")
    parser.add_argument("--production", action="store_true", help="judge the settings as APP_ENV=production")
    args = parser.parse_args(argv)

    from app.core.config import Settings
    from src.ai.mcp_server.config import MCPSettings

    if args.env_file:
        settings, providers = _only_from(Settings, args.env_file), _only_from(MCPSettings, args.env_file)
    else:
        settings, providers = Settings(), MCPSettings()
    if args.production:
        settings.APP_ENV = "production"

    report = check(settings, providers)
    print(f"[PREFLIGHT] APP_ENV={settings.APP_ENV}")
    print(report.text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

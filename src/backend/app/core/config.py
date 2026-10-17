"""
Central settings object.
Access anywhere: from app.core.config import settings
"""

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[4]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,  # anchored to the repo root, so the working directory doesn't matter
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    APP_ENV: str = "development"
    APP_PORT: int = 8000

    # Phase 27 — a hosted Postgres's URL is taken as it is handed out (sslmode=…, channel_binding=…,
    # postgres://…): src/backend/app/db/url.py makes of it what each driver needs. The pooled address
    # (Neon: the host with "-pooler") is the one to give here.
    DATABASE_URL: str = "postgresql://tripplanner:tripplanner_secret@localhost:5432/tripplanner_db"
    # Optional: the same database's direct, un-pooled address. Only the migrations use it — a pooler
    # in transaction mode is the wrong road for anything that needs one session from start to end.
    DATABASE_URL_UNPOOLED: str = ""
    # Phase 30 — how many database connections this process may hold: DB_POOL_SIZE are kept open, and
    # up to DB_MAX_OVERFLOW more are opened when those are all in use and closed when they are given
    # back. Past the two together a request waits for a connection — 30 s, then it fails. The defaults
    # are SQLAlchemy's own, which is what the app ran on until a load test said what they meant
    # (docs/load-test-report.md). Behind a pooler, every instance's two numbers have to fit in what the
    # pooler lends one user of one database — and two instances are alive at once during a deploy.
    DB_POOL_SIZE: int = Field(default=5, ge=1)
    DB_MAX_OVERFLOW: int = Field(default=10, ge=0)
    # Redis over TCP: redis://, or rediss:// (TLS) for a hosted one. Never a REST URL — the event
    # stream is a subscription, and a REST API cannot subscribe.
    REDIS_URL: str = "redis://localhost:6379"
    # Phase 30 — how many connections to Redis this process may open. Every open trip page keeps one
    # for as long as it is open (its event stream is a subscription), and every command borrows one
    # for a moment — so this is, near enough, how many pages one instance can have open at once. Past
    # it nothing waits: the next command fails ("Too many connections"), and with it whatever needed
    # it. The default is the Redis library's own, which the app ran on unsaid until a load test opened
    # the 101st page. A hosted Redis has a limit of concurrent connections of its own: stay under it.
    REDIS_MAX_CONNECTIONS: int = Field(default=100, ge=1)

    # Comma-separated browser origins allowed by CORS (Phase 17). In production: the frontend's
    # origin — the page opens its event stream straight from the browser to this API.
    CORS_ORIGINS: str = "http://localhost:3000"
    # Origins allowed by pattern as well (Phase 27) — for a host that gives every deployment an
    # address of its own, e.g. https://tripplanner-[a-z0-9-]+\.vercel\.app. Empty: only the list above.
    CORS_ORIGIN_REGEX: str = ""

    # JWT (Phase 5)
    JWT_SECRET: str = "change-me-before-production"
    JWT_ALGORITHM: str = "HS256"
    # Phase 35: an hour (it was a day) — a token that leaks is good for that long; staying signed in is
    # the refresh token's job (POST /auth/refresh): REFRESH_TOKEN_DAYS each, used once, and never past
    # REFRESH_SESSION_DAYS after the sign-in. A refresh token presented again more than
    # REFRESH_REUSE_GRACE_S after it was used ends its whole sign-in (two parties have it).
    JWT_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_DAYS: int = 14
    REFRESH_SESSION_DAYS: int = 30
    REFRESH_REUSE_GRACE_S: int = 10
    # The browser keeps its refresh token in an HttpOnly cookie that only the sign-in routes are sent —
    # as the browser sees them, through the site's /api proxy.
    REFRESH_COOKIE_PATH: str = "/api/auth"

    # Phase 35: how many times one address may try to sign in, or make an account, in any 60 seconds
    # (app/core/ratelimit.py). 0: no limit — for a stack that is loaded from one address on purpose.
    LOGIN_ATTEMPTS_PER_MINUTE: int = 5
    REGISTRATIONS_PER_MINUTE: int = 5

    # LLMs (Phase 6+) — model IDs are settings so a retired model is an env change, not a code change
    GOOGLE_API_KEY: str = ""  # Gemini: intent parsing, refinement classifier, embeddings
    GEMINI_MODEL: str = "gemini-3.5-flash"
    GROQ_API_KEY: str = ""  # itinerary builder, preference extractor
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    GROQ_SMALL_MODEL: str = "openai/gpt-oss-20b"  # stands in for Gemini when it is unavailable (src/ai/llm.py)

    # Phase 24 — POST /trips starts the trip's opening searches in the background, so planning finds
    # them cached (src/ai/orchestrator/warming.py). Each trip created costs one flight search, which
    # the provider counts: turn this off where the quota is too tight to spend on trips never planned.
    CACHE_WARMING_ENABLED: bool = True

    # Phase 26 — LangSmith tracing. Off unless switched on here *and* given a key: every run of the
    # planner is then uploaded as a trace, tagged with its trip and its user (src/ai/tracing.py).
    # A trace holds what the planner saw and wrote — the destination, the dates, the travellers'
    # names and interests, the plan — so switching this on sends that to LangSmith.
    # LANGCHAIN_ENDPOINT is for another LangSmith (the EU one, a self-hosted one) or the local
    # stand-in, scripts/trace_receiver.py; empty means LangSmith's own.
    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_API_KEY: str = ""
    LANGCHAIN_PROJECT: str = "tripplanner-ai"
    LANGCHAIN_ENDPOINT: str = ""

    # Phase 26 — what the models cost. Every agent_runs row of a run that asked a model carries its
    # tokens and their price (src/ai/usage.py). When one trip's runs add up to more than this many
    # dollars, a warning is logged: at the models' list prices a plan is well under a cent, so
    # this is the sign of a loop, not of a long trip. 0 switches the warning off.
    LLM_COST_ALERT_USD: float = 0.10
    # Prices to add to, or put in place of, the ones in src/ai/usage.py — USD per million tokens,
    # as JSON: {"gemini-3.5-flash": [0.50, 3.00]} is that model's input and output price.
    LLM_PRICES: dict[str, tuple[float, float]] = {}

    # Phase 27 — errors reported to Sentry. Empty: nothing is reported (src/backend/app/core/error_reporting.py
    # says what a report may and may not carry). One request in ten is also sent as a performance trace.
    SENTRY_DSN: str = ""
    SENTRY_TRACES_SAMPLE_RATE: float = 0.1
    # A secret for operating the deployment. It opens two doors: GET /admin/sentry-test (as the
    # X-Ops-Token header), which fails on purpose so that error reporting can be seen to work — empty,
    # that route does not exist (404) — and GET /metrics (Phase 29, as `Authorization: Bearer …`, which
    # is what Prometheus sends): set, the metrics are for whoever has it; empty, they are open in
    # development and closed in production. Set: openssl rand -hex 32
    OPS_TOKEN: str = ""

    # Phase 29 — what the running system says about itself (src/telemetry/).
    # LOG_FORMAT: "json" makes every log line one JSON object carrying trip_id, user_id and the trace
    # it belongs to — what a log store searches by; "text" is the console as a person reads it.
    # Empty: json where APP_ENV is production, text anywhere else.
    LOG_FORMAT: str = ""
    # Where traces are sent, as OTLP over HTTP: Jaeger's http://localhost:4318 (scripts/observability.py
    # starts one), or a hosted backend's address. Empty: nothing is traced and nothing is sent.
    # A trace holds what the system did — which requests, which SQL (never its values), which Redis
    # commands (never their arguments), which tools and models and how long each took — tagged with
    # the trip's id and the user's. Not what was asked or answered: that is LangSmith's, above.
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    # Headers for that endpoint, as name=value,name=value — a hosted backend's key goes here.
    OTEL_EXPORTER_OTLP_HEADERS: str = ""
    # The share of requests traced, 0 to 1. A health check never is.
    OTEL_TRACES_SAMPLE_RATIO: float = 1.0
    # Phase 28 — which commit this deployment was built from. The pipeline that deploys sets it, and
    # GET /version says it: that is how a deploy is known to be live, and not merely to have been started.
    # (A platform that builds from GitHub itself says the same under a name of its own — see `commit`.)
    GIT_COMMIT_SHA: str = ""

    # How long a stopping server waits for the plans it is making before it gives them up (Phase 27).
    # A platform's own grace period (Railway: drainingSeconds) must be longer than this.
    SHUTDOWN_GRACE_SECONDS: float = 40.0

    # Map tiles for the static map in the exported PDF (Phase 19) — the backend's counterpart of the
    # frontend's NEXT_PUBLIC_MAP_TILE_URL. OpenStreetMap's own server needs no key and is fine for
    # development; a deployment points this at a tile provider it has an account with. Empty = no map.
    MAP_TILE_URL: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    MAP_ATTRIBUTION: str = "© OpenStreetMap contributors"

    @property
    def commit(self) -> str:
        """The commit this is running, as far as anyone has said: ours, else Railway's own, else nothing."""
        return self.GIT_COMMIT_SHA or os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


settings = Settings()

# The Gemini and Groq SDKs read their keys from os.environ, which
# pydantic-settings does not populate. Export exactly these — not the whole
# .env, which would also switch on things like LangSmith tracing.
for _key in ("GOOGLE_API_KEY", "GROQ_API_KEY"):
    if getattr(settings, _key):
        os.environ.setdefault(_key, getattr(settings, _key))

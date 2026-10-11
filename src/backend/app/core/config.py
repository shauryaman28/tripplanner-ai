"""
Central settings object.
Access anywhere: from app.core.config import settings
"""

import os
from pathlib import Path

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

    DATABASE_URL: str = "postgresql://tripplanner:tripplanner_secret@localhost:5432/tripplanner_db"
    REDIS_URL: str = "redis://localhost:6379"

    # Comma-separated browser origins allowed by CORS (Phase 17)
    CORS_ORIGINS: str = "http://localhost:3000"

    # JWT (Phase 5)
    JWT_SECRET: str = "change-me-before-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440

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

    # Map tiles for the static map in the exported PDF (Phase 19) — the backend's counterpart of the
    # frontend's NEXT_PUBLIC_MAP_TILE_URL. OpenStreetMap's own server needs no key and is fine for
    # development; a deployment points this at a tile provider it has an account with. Empty = no map.
    MAP_TILE_URL: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    MAP_ATTRIBUTION: str = "© OpenStreetMap contributors"

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

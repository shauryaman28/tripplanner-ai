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

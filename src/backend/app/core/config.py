"""
Central settings object.
Access anywhere: from app.core.config import settings
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    APP_ENV: str = "development"
    APP_PORT: int = 8000

    DATABASE_URL: str = (
        "postgresql://tripplanner:tripplanner_secret@localhost:5432/tripplanner_db"
    )
    REDIS_URL: str = "redis://localhost:6379"

    # JWT (Phase 5)
    JWT_SECRET: str = "change-me-before-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440

    # LLMs (Phase 6+)
    GOOGLE_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""

    # External APIs (Phase 3)
    AMADEUS_CLIENT_ID: str = ""
    AMADEUS_CLIENT_SECRET: str = ""
    AMADEUS_BASE_URL: str = "https://test.api.amadeus.com"
    AMADEUS_HOTEL_ENABLED: bool = True
    GOOGLE_MAPS_API_KEY: str = ""
    OPENWEATHER_API_KEY: str = ""

    # Observability (Phase 26+)
    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_API_KEY: str = ""
    LANGCHAIN_PROJECT: str = "tripplanner-ai"
    SENTRY_DSN: str = ""


settings = Settings()

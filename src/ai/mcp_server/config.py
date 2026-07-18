"""Standalone settings for the MCP server.

Deliberately separate from src/backend/app/core/config.py so the MCP
server can run independently of the FastAPI app (different process, no
shared imports).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    REDIS_URL: str = "redis://localhost:6379"

    # Amadeus (flights + hotels)
    AMADEUS_CLIENT_ID: str = ""
    AMADEUS_CLIENT_SECRET: str = ""
    AMADEUS_BASE_URL: str = "https://test.api.amadeus.com"

    # Google Maps (attractions)
    GOOGLE_MAPS_API_KEY: str = ""

    # OpenWeatherMap (weather)
    OPENWEATHER_API_KEY: str = ""


mcp_settings = MCPSettings()

"""Standalone settings for the MCP server.

Deliberately separate from src/backend/app/core/config.py so the MCP
server can run independently of the FastAPI app (different process, no
shared imports).
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env",  # repo root, whatever the CWD
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    REDIS_URL: str = "redis://localhost:6379"

    # Duffel (flights)
    DUFFEL_ACCESS_TOKEN: str = ""

    # LiteAPI (hotels)
    LITEAPI_API_KEY: str = ""

    # OpenTripMap (attractions)
    OPENTRIPMAP_API_KEY: str = ""

    # OpenWeatherMap (weather)
    OPENWEATHER_API_KEY: str = ""


mcp_settings = MCPSettings()

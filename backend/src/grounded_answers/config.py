import functools
from pathlib import Path
from typing import Literal

from pydantic import PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    If a required variable is missing, Pydantic raises ValidationError at startup
    (before the app starts listening). This ensures misconfigurations are caught
    early, not at runtime when a request hits an endpoint.

    PostgresDsn and RedisDsn validate URL format (scheme, host, port, etc.) and
    reject malformed URLs with ValidationError. log_level is a Literal type:
    only "debug", "info", "warning", "error", "critical" are accepted.
    """

    database_url: PostgresDsn
    redis_url: RedisDsn
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    cors_origins: str = "http://127.0.0.1:3000,http://localhost:3000"

    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent.parent.parent / ".env",
        env_file_encoding="utf-8",
    )

    def get_cors_origins(self) -> list[str]:
        """Parse comma-separated CORS_ORIGINS into a list."""
        return [origin.strip() for origin in self.cors_origins.split(",")]


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build Settings once per process via lru_cache; returns cached instance on repeat calls."""
    return Settings()

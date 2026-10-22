import functools
from pathlib import Path
from typing import Literal

from pydantic import PostgresDsn, RedisDsn, SecretStr
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
    # Dev host is 127.0.0.1 everywhere (see ADR 0004). localhost is a different
    # host from 127.0.0.1, so cookies set by one are not sent to the other.
    cors_origins: str = "http://127.0.0.1:3000"
    # Auth (ADR 0004). The key file holds Ed25519 private keys; it lives outside the repo and is
    # read only by the API process. AUTH_JWT_ACTIVE_KID, if set, must match the file's "active".
    auth_jwt_keys_file: Path | None = None
    auth_jwt_active_kid: str | None = None
    # Keys the HMAC'd client IP in the audit log (ADR 0004). Required at startup, never logged.
    auth_log_hmac_key: SecretStr | None = None

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

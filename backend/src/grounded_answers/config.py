import functools
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr
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
    # Keys the Valkey limit keys (ADR 0014). Separate from the log key, so rotating one leaves the other.
    auth_rate_limit_hmac_key: SecretStr | None = None
    # Minimum duration of every login response, in milliseconds (ADR 0014, R11). Bounded so a typo
    # cannot make logins instant (timing leak) or unusably slow.
    auth_login_floor_ms: int = Field(default=500, ge=100, le=5000)
    # Document upload (ADR 0019). Off until the worker runs the ingestion task (Step 6): the
    # endpoint then answers 404, so the switch cannot queue jobs nobody runs.
    upload_enabled: bool = False
    # Days a deleted document or a replaced version is kept before the purge removes it (ADR 0027). The worker reads
    # the same variable (worker_config.py), so the two must agree.
    document_retention_days: int = Field(default=30, ge=1, le=365)
    # How many chunks retrieval returns to the answer step (ADR 0030). 5 and 8 are compared in the answer evaluation.
    retrieval_top_k: int = Field(default=5, ge=1, le=20)

    # POST /ask (ADR 0033). Off by default: with it off the endpoint answers 404 and the web process loads no
    # embedding model, so development and CI stay light. On, EMBEDDING_MODEL_DIR is required (the pinned model
    # directory, from `python -m grounded_answers.embedding.fetch`) and the process holds about 0.76 GB more.
    ask_enabled: bool = False
    embedding_model_dir: Path | None = None
    # How many questions are embedded at the same time (ADR 0033, decision 7). Each takes two CPU threads.
    embed_concurrency: int = Field(default=2, ge=1, le=4)

    # POST /ask limits (ADR 0033, decisions 3 and 6). Per minute, counted per attempt; the in-flight limit is how many
    # questions of one tenant may be in progress at once; the deadline is the whole request budget in seconds.
    ask_rate_user_per_min: int = Field(default=5, ge=1, le=1000)
    ask_rate_tenant_per_min: int = Field(default=20, ge=1, le=10000)
    ask_inflight_per_tenant: int = Field(default=2, ge=1, le=20)
    ask_deadline_seconds: float = Field(default=12.0, ge=5.0, le=30.0)

    # Answer-model API keys (ADR 0031). Read from backend/.env only, held as SecretStr so they never print.
    # Groq and Gemini free tiers take fictional data only. A real client's key is their own paid, no-training key.
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

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

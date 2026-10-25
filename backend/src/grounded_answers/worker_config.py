"""
Settings for the worker process only (ADR 0017).

The worker logs in as worker_user, which can delete documents and their stored bytes (ADR 0007).
Its URL is therefore read from the environment of the worker process, never from a file, and
the web app's Settings never declare it. env_file=None means no .env file is read at all.
"""

from pathlib import Path

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """The worker's settings, all from the shell environment (ADR 0017, ADR 0022)."""

    worker_database_url: PostgresDsn
    # The pinned model directory, from `python -m grounded_answers.embedding.fetch` (ADR 0022, D6). Optional here
    # so the settings can be read without it. The worker refuses to start without it (jobs/worker.py).
    worker_embedding_model_dir: Path | None = None
    # How long an attempt holds a version before another attempt may take it over (ADR 0022, D1).
    worker_processing_lease_seconds: int = Field(default=1200, ge=120, le=7200)
    # Days a replaced or deleted version is kept before the purge removes it (ADR 0027). The same variable as the web
    # app reads (Settings.document_retention_days). The swap stamps purge_after with it.
    document_retention_days: int = Field(default=30, ge=1, le=365)

    model_config = SettingsConfigDict(env_file=None, extra="forbid")

"""
Settings for the worker process only (ADR 0017).

The worker logs in as worker_user, which can delete documents and their stored bytes (ADR 0007).
Its URL is therefore read from the environment of the worker process, never from a file, and
the web app's Settings never declare it. env_file=None means no .env file is read at all.
"""

from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """The worker's database login. Set WORKER_DATABASE_URL in the shell, not in a file."""

    worker_database_url: PostgresDsn

    model_config = SettingsConfigDict(env_file=None, extra="forbid")

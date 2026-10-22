"""
backend/.env.example must load into Settings (CI guard for the example file).

Settings forbids unknown keys (pydantic-settings default extra="forbid"), so a key the example
has and Settings lacks, such as a migration URL, fails here instead of at app startup.
"""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from grounded_answers.config import Settings

EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

# Every name Settings reads from the environment or a file. Cleared so the example is the
# only source (the autouse fixture in conftest.py sets DATABASE_URL and REDIS_URL, and
# environment variables override the file).
SETTINGS_NAMES = ("DATABASE_URL", "REDIS_URL", "LOG_LEVEL", "CORS_ORIGINS")


@pytest.fixture
def clean_environment(monkeypatch: MonkeyPatch) -> None:
    for name in (*SETTINGS_NAMES, "MIGRATION_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_env_example_loads_into_settings(clean_environment: None) -> None:
    """Every key in the example is a Settings field, and the values validate."""
    settings = Settings(_env_file=EXAMPLE)
    assert str(settings.database_url).startswith("postgresql+psycopg://app_user:")
    assert str(settings.redis_url) == "redis://127.0.0.1:6379/0"
    assert settings.get_cors_origins() == ["http://127.0.0.1:3000"]


def test_env_example_has_no_migration_credentials() -> None:
    """Migration credentials never sit in the file the web app loads (ADR 0004)."""
    assert not re.search(r"^\s*MIGRATION_", EXAMPLE.read_text(encoding="utf-8"), re.MULTILINE)


def test_settings_rejects_unknown_key_in_env_file(tmp_path: Path, clean_environment: None) -> None:
    """The failure this guard exists for: an extra key in the env file stops startup."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        EXAMPLE.read_text(encoding="utf-8")
        + "MIGRATION_DATABASE_URL=postgresql+psycopg://migration_role:x@127.0.0.1:5432/db\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="migration_database_url"):
        Settings(_env_file=env_file)

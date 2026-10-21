from collections.abc import Generator

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from grounded_answers.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _reset_settings_and_env(monkeypatch: MonkeyPatch) -> Generator[None, None, None]:
    """
    Autouse fixture: set required env vars and clear get_settings cache before/after each test.

    This ensures tests don't depend on backend/.env and work in CI/CD environments.
    """
    get_settings.cache_clear()

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    yield

    get_settings.cache_clear()


def test_settings_loads_from_env() -> None:
    """Settings loads required variables from environment."""
    settings = Settings(_env_file=None)
    assert str(settings.database_url) == "postgresql+psycopg://user:pass@localhost/db"
    assert str(settings.redis_url) == "redis://localhost:6379/0"
    assert settings.log_level == "info"


def test_settings_missing_required_raises(monkeypatch: MonkeyPatch) -> None:
    """Settings raises ValidationError if required variables are missing."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    errors = exc_info.value.errors()
    assert len(errors) >= 2
    assert any(e["loc"] == ("database_url",) for e in errors)
    assert any(e["loc"] == ("redis_url",) for e in errors)


def test_settings_log_level_defaults(monkeypatch: MonkeyPatch) -> None:
    """log_level defaults to 'info' if not provided."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    settings = Settings(_env_file=None)
    assert settings.log_level == "info"


def test_settings_invalid_log_level_rejected(monkeypatch: MonkeyPatch) -> None:
    """Invalid log_level value is rejected by Literal validator."""
    monkeypatch.setenv("LOG_LEVEL", "invalid_level")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    errors = exc_info.value.errors()
    assert any(e["loc"] == ("log_level",) for e in errors)


def test_settings_malformed_redis_url_rejected(monkeypatch: MonkeyPatch) -> None:
    """Malformed REDIS_URL is rejected by RedisDsn validator."""
    monkeypatch.setenv("REDIS_URL", "not-a-valid-url")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    errors = exc_info.value.errors()
    assert any(e["loc"] == ("redis_url",) for e in errors)


def test_settings_malformed_database_url_rejected(monkeypatch: MonkeyPatch) -> None:
    """Malformed DATABASE_URL is rejected by PostgresDsn validator."""
    monkeypatch.setenv("DATABASE_URL", "not-a-valid-postgres-url")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    errors = exc_info.value.errors()
    assert any(e["loc"] == ("database_url",) for e in errors)


def test_get_settings_cached() -> None:
    """get_settings() returns same cached instance on repeated calls."""
    settings1 = get_settings()
    settings2 = get_settings()

    assert settings1 is settings2

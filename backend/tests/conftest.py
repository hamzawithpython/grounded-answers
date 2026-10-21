from collections.abc import Generator

import pytest
from pytest import MonkeyPatch

from grounded_answers.config import get_settings


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

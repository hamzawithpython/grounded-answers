from collections.abc import Generator
from typing import Any

import pytest
from pytest import MonkeyPatch

from grounded_answers.config import get_settings
from grounded_answers.runtime import selector_loop_factory


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    """
    Run anyio-marked async tests on a selector loop (psycopg async, ADR 0005).

    Without this, Windows runs them on ProactorEventLoop and psycopg fails.
    """
    return "asyncio", {"loop_factory": selector_loop_factory}


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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Tests marked `eval` need the real model and take minutes, so they run only when -m names `eval`.

    Without this, `-m "not db"` would collect them. They are deselected, not skipped: nothing is hidden when
    the marker is asked for."""
    if "eval" in (config.getoption("markexpr") or ""):
        return
    dropped = [item for item in items if item.get_closest_marker("eval") is not None]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = [item for item in items if item not in dropped]

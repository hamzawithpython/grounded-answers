import asyncio
from typing import Any, cast

import anyio
import pytest
from uvicorn.config import Config

from grounded_answers.runtime import ensure_psycopg_compatible_loop, selector_loop_factory


class ProactorEventLoop:
    """Stand-in for Windows' ProactorEventLoop. The check works on class name, so this runs everywhere."""


def test_selector_loop_factory_returns_selector_loop() -> None:
    """The factory builds a selector loop, the only kind psycopg async supports on Windows."""
    loop = selector_loop_factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_check_rejects_proactor_loop_with_fix_in_message() -> None:
    """A Proactor loop fails startup with the exact uvicorn flag to use."""
    fake_loop = cast(asyncio.AbstractEventLoop, ProactorEventLoop())
    with pytest.raises(RuntimeError, match="selector event loop") as excinfo:
        ensure_psycopg_compatible_loop(fake_loop)
    assert "grounded_answers.runtime:selector_loop_factory" in str(excinfo.value)


def test_check_accepts_selector_loop() -> None:
    """A selector loop passes the check without raising."""
    loop = selector_loop_factory()
    try:
        ensure_psycopg_compatible_loop(loop)
    finally:
        loop.close()


async def _running_loop_is_selector() -> bool:
    return isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)


def test_anyio_runs_on_selector_loop() -> None:
    """anyio with our backend option runs the coroutine on a selector loop."""
    result = anyio.run(
        _running_loop_is_selector,
        backend="asyncio",
        backend_options={"loop_factory": selector_loop_factory},
    )
    assert result is True


def test_anyio_backend_fixture_uses_selector_factory(
    anyio_backend: tuple[str, dict[str, Any]],
) -> None:
    """The conftest fixture hands pytest-anyio the selector factory."""
    backend, options = anyio_backend
    assert backend == "asyncio"
    assert options["loop_factory"] is selector_loop_factory


def test_uvicorn_resolves_runbook_loop_string() -> None:
    """The --loop string in docs/RUNBOOK.md resolves to our factory (fails if the path is wrong)."""
    config = Config(
        "grounded_answers.main:create_app",
        factory=True,
        loop="grounded_answers.runtime:selector_loop_factory",
    )
    assert config.get_loop_factory() is selector_loop_factory

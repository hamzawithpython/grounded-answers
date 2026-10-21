"""
Event-loop setup for async database access (ADR 0005).

psycopg 3 async mode does not work on Windows' default ProactorEventLoop
(psycopg docs: "On Windows, Psycopg is not compatible with the default
ProactorEventLoop"). uvicorn picks a Proactor loop on Windows unless it runs a
subprocess (for example --reload), so the dev server must pass this factory
explicitly:

    uv run uvicorn grounded_answers.main:create_app --factory \
        --loop grounded_answers.runtime:selector_loop_factory

On Linux and macOS the default loop is already a selector loop, so this
factory returns the same loop type there.
"""

import asyncio


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """
    Return a new SelectorEventLoop.

    Used as uvicorn's --loop factory and as anyio's backend option, so the dev
    server and the test runner use the same loop type.
    """
    return asyncio.SelectorEventLoop()


def ensure_psycopg_compatible_loop(loop: asyncio.AbstractEventLoop) -> None:
    """
    Fail fast with a clear message if the running loop cannot run psycopg async.

    Called where the database engine is created, so a wrong loop stops startup
    instead of failing on the first query. The check is by class name so it
    works on every platform and can be tested without Windows.

    Raises:
        RuntimeError: if the loop is a Proactor loop.
    """
    if "Proactor" in type(loop).__name__:
        raise RuntimeError(
            f"psycopg async needs a selector event loop, got {type(loop).__name__}. "
            "Start uvicorn with "
            "--loop grounded_answers.runtime:selector_loop_factory "
            "(see docs/RUNBOOK.md)."
        )

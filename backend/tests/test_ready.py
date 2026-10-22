"""
GET /ready and ReadinessChecker, with fake checks (ADR 0009). No database or Valkey needed.

The real checks are tested in tests/db/test_readiness_db.py against the test database.
"""

import asyncio
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from grounded_answers.main import create_app
from grounded_answers.readiness import (
    CACHE,
    DATABASE,
    SCHEMA,
    CheckFn,
    ReadinessChecker,
    SchemaVersionMismatchError,
)


async def _ok() -> None:
    return None


def _raising(exc: BaseException) -> CheckFn:
    async def check() -> None:
        raise exc

    return check


def _hanging() -> CheckFn:
    async def check() -> None:
        await asyncio.sleep(10)

    return check


def _all_ok() -> dict[str, CheckFn]:
    return {DATABASE: _ok, CACHE: _ok, SCHEMA: _ok}


def _client(checks: dict[str, CheckFn], **options: Any) -> TestClient:
    checker = ReadinessChecker(checks, **options)
    return TestClient(create_app(readiness=checker))


def test_ready_when_every_check_passes() -> None:
    """200 with exactly {"status": "ready"}, and nothing about the dependencies."""
    with _client(_all_ok()) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_database_down_returns_503_naming_only_database() -> None:
    checks = _all_ok() | {DATABASE: _raising(OSError("connection refused"))}
    with _client(checks) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": [DATABASE]}


def test_cache_down_returns_503_naming_only_cache() -> None:
    checks = _all_ok() | {CACHE: _raising(RedisConnectionError("refused"))}
    with _client(checks) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": [CACHE]}


def test_schema_behind_code_returns_503_naming_only_schema() -> None:
    mismatch = SchemaVersionMismatchError(found="0003_policies_and_definers", expected="0004")
    checks = _all_ok() | {SCHEMA: _raising(mismatch)}
    with _client(checks) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": [SCHEMA]}


def test_timeout_path_returns_quickly_and_names_the_slow_check() -> None:
    """A hanging check is cut off by its timeout, so the response is fast."""
    checks = _all_ok() | {CACHE: _hanging()}
    with _client(checks, check_timeout=0.05, budget=1.0) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": [CACHE]}


def test_overall_budget_caps_the_whole_check_run() -> None:
    """If the budget runs out, every check counts as failed rather than the request hanging."""
    checks = {DATABASE: _hanging(), CACHE: _hanging(), SCHEMA: _hanging()}
    with _client(checks, check_timeout=10.0, budget=0.05) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": [DATABASE, CACHE]}


def test_schema_is_not_named_when_database_is_the_root_cause() -> None:
    """With no database, the schema check fails too. Only the root cause is named."""
    checks = {
        DATABASE: _raising(OSError("refused")),
        CACHE: _ok,
        SCHEMA: _raising(OSError("refused")),
    }
    with _client(checks) as client:
        response = client.get("/ready")
    assert response.json() == {"status": "not_ready", "failed": [DATABASE]}


def test_failure_detail_is_logged_without_secrets(caplog: pytest.LogCaptureFixture) -> None:
    """The log names the failed check and the exception class. It never includes the message."""
    internal_host = "db-internal-7f3a.example.internal"
    checks = _all_ok() | {DATABASE: _raising(OSError(f"could not reach {internal_host}"))}
    with caplog.at_level(logging.WARNING, logger="grounded_answers.readiness"):
        with _client(checks) as client:
            client.get("/ready")
    assert "readiness check failed: database (OSError)" in caplog.text
    assert internal_host not in caplog.text


def test_schema_mismatch_is_logged_with_versions(caplog: pytest.LogCaptureFixture) -> None:
    checks = _all_ok() | {SCHEMA: _raising(SchemaVersionMismatchError("0003", "0004"))}
    with caplog.at_level(logging.WARNING, logger="grounded_answers.readiness"):
        with _client(checks) as client:
            client.get("/ready")
    assert "version mismatch: found 0003, expected 0004" in caplog.text


def test_repeated_calls_within_ttl_do_not_recheck() -> None:
    """Load control: a public endpoint must not open a new connection per request."""
    calls = {"n": 0}

    async def counting_db() -> None:
        calls["n"] += 1

    clock = [100.0]
    checker = ReadinessChecker({DATABASE: counting_db}, cache_ttl=2.0, clock=lambda: clock[0])

    async def run() -> None:
        for _ in range(5):
            assert await checker.failed_dependencies() == ()

    asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop)
    assert calls["n"] == 1


def test_check_runs_again_after_ttl_expires() -> None:
    calls = {"n": 0}

    async def counting_db() -> None:
        calls["n"] += 1

    clock = [100.0]
    checker = ReadinessChecker({DATABASE: counting_db}, cache_ttl=2.0, clock=lambda: clock[0])

    async def run() -> None:
        await checker.failed_dependencies()
        clock[0] = 102.5
        await checker.failed_dependencies()

    asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop)
    assert calls["n"] == 2


def test_failed_result_is_cached_too() -> None:
    """A down dependency is also cached, so a dead database is not hammered by retries."""
    calls = {"n": 0}

    async def failing_db() -> None:
        calls["n"] += 1
        raise OSError("refused")

    checker = ReadinessChecker({DATABASE: failing_db}, cache_ttl=2.0, clock=lambda: 100.0)

    async def run() -> None:
        for _ in range(3):
            assert await checker.failed_dependencies() == (DATABASE,)

    asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop)
    assert calls["n"] == 1


def test_checks_run_concurrently() -> None:
    """Three 0.3 s checks finish in about 0.3 s, not 0.9 s."""

    async def slow() -> None:
        await asyncio.sleep(0.3)

    checker = ReadinessChecker(
        {DATABASE: slow, CACHE: slow, SCHEMA: slow}, check_timeout=2.0, budget=2.0
    )

    async def timed() -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        assert await checker.failed_dependencies() == ()
        return loop.time() - start

    elapsed = asyncio.run(timed(), loop_factory=asyncio.SelectorEventLoop)
    assert elapsed < 0.8

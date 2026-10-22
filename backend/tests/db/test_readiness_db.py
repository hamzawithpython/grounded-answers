"""
Real readiness checks against the test database and Valkey (ADR 0009, ADR 0006).

These run the same check functions the app runs, as app_user. A pass here proves the grant in
migration 0004 and the login path work. They need the grounded_db fixture (PostgreSQL) and a
Valkey on TEST_REDIS_URL (CI: service container; local: docker compose).
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from grounded_answers.database import create_database_engine
from grounded_answers.readiness import (
    CACHE,
    DATABASE,
    SCHEMA,
    ReadinessChecker,
    make_cache_check,
    make_database_check,
    make_schema_check,
)

from .database import DbHandle

pytestmark = pytest.mark.db

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/0")


@asynccontextmanager
async def _app_engine(db: DbHandle) -> AsyncIterator[AsyncEngine]:
    """Engine logged in as app_user to the test database, disposed when the block ends."""
    engine = create_database_engine(db.url_for("app_user", db.passwords["app_user"]))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_all_real_checks_pass_when_healthy(grounded_db: DbHandle) -> None:
    """app_user reaches the database, Valkey answers, and the schema is at the expected head."""
    cache = Redis.from_url(TEST_REDIS_URL)
    try:
        async with _app_engine(grounded_db) as engine:
            # Generous limits for this healthy-path test only, so a loaded machine or CI runner
            # cannot fail it. Production keeps the 1 s check timeout and 2 s budget, and the
            # timeout tests below keep them strict.
            checker = ReadinessChecker(
                {
                    DATABASE: make_database_check(engine),
                    CACHE: make_cache_check(cache),
                    SCHEMA: make_schema_check(engine),
                },
                check_timeout=10.0,
                budget=15.0,
            )
            assert await checker.failed_dependencies() == ()
    finally:
        await cache.aclose()


@pytest.mark.anyio
async def test_schema_check_fails_when_expected_head_differs(grounded_db: DbHandle) -> None:
    """The real version read is compared with the expected head, so a stale head is caught."""
    async with _app_engine(grounded_db) as engine:
        checker = ReadinessChecker(
            {SCHEMA: make_schema_check(engine, expected="0003_policies_and_definers")}
        )
        assert await checker.failed_dependencies() == (SCHEMA,)


@pytest.mark.anyio
async def test_database_down_is_named_and_schema_is_not(grounded_db: DbHandle) -> None:
    """Port 1 refuses connections at once, so this is a real 'database down' result."""
    dead = create_database_engine(
        "postgresql+psycopg://app_user:unused@127.0.0.1:1/grounded_answers_test"
    )
    try:
        checker = ReadinessChecker(
            {DATABASE: make_database_check(dead), SCHEMA: make_schema_check(dead)}
        )
        assert await checker.failed_dependencies() == (DATABASE,)
    finally:
        await dead.dispose()


@pytest.mark.anyio
async def test_cache_down_is_named(grounded_db: DbHandle) -> None:
    """Port 1 refuses connections, so Valkey is down for this check."""
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.5)
    try:
        async with _app_engine(grounded_db) as engine:
            checker = ReadinessChecker(
                {DATABASE: make_database_check(engine), CACHE: make_cache_check(dead)}
            )
            assert await checker.failed_dependencies() == (CACHE,)
    finally:
        await dead.aclose()

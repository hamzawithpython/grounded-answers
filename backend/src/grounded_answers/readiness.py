"""
Readiness checks for GET /ready (ADR 0009).

Liveness (/health) only says the process runs. Readiness says the app can serve requests:
PostgreSQL answers as app_user, Valkey answers PING, and the schema is at the migration head
this code expects. A schema behind the code counts as not ready.

Design:
- Each check is a plain async function that raises on failure. Tests pass fakes; the
  functions below are the real ones.
- Every check has its own timeout, and all checks together share an overall budget. A slow
  dependency cannot hold the request open.
- The result is cached for a short time. /ready is public, so without the cache every
  caller would open new database connections.
- The public response names only the failed dependency. Details go to the log, and the log
  records exception class names, never messages (messages can contain hosts or credentials).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

# Head revision of the migrations this code expects. A test checks it against alembic/versions,
# so adding a migration without updating this constant fails CI (ADR 0009).
EXPECTED_SCHEMA_HEAD = "0013_replace_and_swap"

# Names used in the public 503 body. Keep them stable: clients may match on them.
DATABASE = "database"
CACHE = "cache"
SCHEMA = "schema"

CheckFn = Callable[[], Awaitable[None]]

logger = logging.getLogger(__name__)


class SchemaVersionMismatchError(Exception):
    """The database is at a different migration head than this code expects."""

    def __init__(self, found: str | None, expected: str) -> None:
        super().__init__("schema version mismatch")
        self.found = found
        self.expected = expected


def make_database_check(engine: AsyncEngine) -> CheckFn:
    """Open a connection as app_user and run SELECT 1."""

    async def check() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    return check


def make_cache_check(client: Redis) -> CheckFn:
    """Send PING to Valkey. redis-py raises on any failure, so a successful return means OK."""

    async def check() -> None:
        await client.ping()

    return check


def make_schema_check(engine: AsyncEngine, expected: str = EXPECTED_SCHEMA_HEAD) -> CheckFn:
    """Read the applied migration version (app_user has SELECT on it, migration 0004)."""

    async def check() -> None:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM grounded.alembic_version"))
            found = result.scalar_one_or_none()
        if found != expected:
            raise SchemaVersionMismatchError(found, expected)

    return check


class ReadinessChecker:
    """Runs the checks concurrently, under timeouts, and caches the outcome briefly."""

    def __init__(
        self,
        checks: Mapping[str, CheckFn],
        *,
        check_timeout: float = 1.0,
        budget: float = 2.0,
        cache_ttl: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._checks = dict(checks)
        self._check_timeout = check_timeout
        self._budget = budget
        self._cache_ttl = cache_ttl
        self._clock = clock
        # One lock: when the cache expires, concurrent requests wait for one check run
        # instead of each starting their own.
        self._lock = asyncio.Lock()
        self._cached: tuple[str, ...] = ()
        self._cached_until: float | None = None

    async def failed_dependencies(self) -> tuple[str, ...]:
        """Return the names of failed dependencies. An empty tuple means ready."""
        async with self._lock:
            now = self._clock()
            if self._cached_until is None or now >= self._cached_until:
                self._cached = await self._run_all()
                self._cached_until = self._clock() + self._cache_ttl
            return self._cached

    async def _run_all(self) -> tuple[str, ...]:
        names = list(self._checks)
        try:
            outcomes = await asyncio.wait_for(
                asyncio.gather(*(self._run_one(name) for name in names)),
                timeout=self._budget,
            )
        except TimeoutError:
            logger.warning("readiness budget of %.1fs exceeded", self._budget)
            return tuple(_report(names))
        return tuple(_report([n for n, ok in zip(names, outcomes, strict=True) if not ok]))

    async def _run_one(self, name: str) -> bool:
        try:
            await asyncio.wait_for(self._checks[name](), timeout=self._check_timeout)
        except TimeoutError:
            logger.warning("readiness check failed: %s (timeout)", name)
            return False
        except SchemaVersionMismatchError as exc:
            logger.warning(
                "readiness check failed: %s (version mismatch: found %s, expected %s)",
                name,
                exc.found,
                exc.expected,
            )
            return False
        except (SQLAlchemyError, RedisError, OSError) as exc:
            logger.warning("readiness check failed: %s (%s)", name, type(exc).__name__)
            return False
        return True


def _report(failed: list[str]) -> list[str]:
    """
    Drop the schema result when the database itself failed.

    With no database, the schema check fails for the same reason. Naming both would hide
    the root cause, so the response names only the database.
    """
    if DATABASE in failed:
        return [name for name in failed if name != SCHEMA]
    return failed

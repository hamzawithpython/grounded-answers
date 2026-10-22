"""
Tenant transactions on pooled connections (R8, ADR 0005, ADR 0004 per-request checks).

Each test uses an engine with one physical connection (pool_size=1, max_overflow=0). Every
transaction therefore runs on the same backend process, so a leaked setting would be visible.
Each test checks that the connection is really the same (pg_backend_pid) before it trusts the
result. The connections log in as app_user, the same login the web app uses.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from grounded_answers.database import create_session_factory
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction
from tests.db.database import DbHandle
from tests.db.seed import Seeded

pytestmark = pytest.mark.db

# What a transaction with no tenant context sees (NULL or empty string, depending on history).
BARE_VIEW_SQL = text(
    "SELECT pg_backend_pid(), "
    "NULLIF(current_setting('app.current_tenant', true), ''), "
    "NULLIF(current_setting('app.current_user', true), ''), "
    "(SELECT count(*) FROM grounded.tenants)"
)


def _app_url(handle: DbHandle) -> str:
    return handle.url_for("app_user", handle.passwords["app_user"]).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )


@asynccontextmanager
async def _one_connection(handle: DbHandle) -> AsyncIterator[AsyncEngine]:
    engine: AsyncEngine = create_async_engine(
        _app_url(handle), pool_size=1, max_overflow=0, pool_pre_ping=True
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _bare_view(factory: async_sessionmaker[AsyncSession]) -> tuple[int, Any, Any, int]:
    """A transaction that does NOT set any context. This is what the next request would see."""
    async with factory() as session, session.begin():
        row = (await session.execute(BARE_VIEW_SQL)).one()
    return (int(row[0]), row[1], row[2], int(row[3]))


async def _tenant_view_pid(
    factory: async_sessionmaker[AsyncSession], context: TenantContext
) -> tuple[int, int]:
    async with begin_tenant_transaction(factory, context) as session:
        pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        visible = (
            await session.execute(text("SELECT count(*) FROM grounded.tenants"))
        ).scalar_one()
    return int(pid), int(visible)


@pytest.mark.anyio
async def test_committed_tenant_does_not_leak_to_the_next_transaction(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    context = TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
    async with _one_connection(grounded_db) as engine:
        factory = create_session_factory(engine)
        pid_a, visible_a = await _tenant_view_pid(factory, context)
        pid_b, tenant_setting, user_setting, visible_b = await _bare_view(factory)

    assert pid_b == pid_a, "the test must reuse the same physical connection"
    assert visible_a == 1, "inside its own tenant, a transaction sees only its tenant row"
    assert tenant_setting is None
    assert user_setting is None
    assert visible_b == 0, "fail closed: no tenant set means no tenant rows"


@pytest.mark.anyio
async def test_rolled_back_tenant_does_not_leak_to_the_next_transaction(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    context = TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
    async with _one_connection(grounded_db) as engine:
        factory = create_session_factory(engine)
        with pytest.raises(RuntimeError, match="request failed"):
            async with begin_tenant_transaction(factory, context) as session:
                await session.execute(text("SELECT 1"))
                raise RuntimeError("request failed")
        pid_b, tenant_setting, user_setting, visible_b = await _bare_view(factory)
        pid_a, _ = await _tenant_view_pid(factory, context)

    assert pid_b == pid_a
    assert tenant_setting is None
    assert user_setting is None
    assert visible_b == 0


@pytest.mark.anyio
async def test_database_error_mid_transaction_does_not_leak(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    """A real error from PostgreSQL (CHECK violation) rolls back and clears the context."""
    context = TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
    async with _one_connection(grounded_db) as engine:
        factory = create_session_factory(engine)
        with pytest.raises(IntegrityError):
            async with begin_tenant_transaction(factory, context) as session:
                # An empty title breaks documents_title_check, so PostgreSQL refuses the row.
                await session.execute(
                    text(
                        "INSERT INTO grounded.documents (id, tenant_id, title) "
                        "VALUES (gen_random_uuid(), :tenant_id, '')"
                    ),
                    {"tenant_id": str(seeded.a.tenant)},
                )
        pid_b, tenant_setting, user_setting, visible_b = await _bare_view(factory)
        pid_a, _ = await _tenant_view_pid(factory, context)

    assert pid_b == pid_a
    assert tenant_setting is None
    assert user_setting is None
    assert visible_b == 0


@pytest.mark.anyio
async def test_context_is_set_before_any_other_statement(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    """The first two statements of a tenant transaction set the context, with bound values."""
    context = TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
    statements: list[tuple[str, Any]] = []

    def _record(conn: Any, cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        statements.append((statement, parameters))

    async with _one_connection(grounded_db) as engine:
        factory = create_session_factory(engine)
        event.listen(engine.sync_engine, "before_cursor_execute", _record)
        try:
            async with begin_tenant_transaction(factory, context) as session:
                await session.execute(text("SELECT 1"))
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _record)

    first_statement, first_parameters = statements[0]
    assert "set_config('app.current_tenant'" in first_statement
    assert "set_config('app.current_user'" in first_statement
    assert str(seeded.a.tenant) not in first_statement, "values must be bound, not interpolated"
    assert first_parameters["tenant_id"] == str(seeded.a.tenant)
    assert first_parameters["user_id"] == str(seeded.a.admin)
    assert "SELECT 1" in statements[1][0]

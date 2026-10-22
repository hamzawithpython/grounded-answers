"""ORM behaviour against the real database, as app_user (ADR 0005, R8)."""

from collections.abc import Generator
from typing import Any

import psycopg
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from grounded_answers.database import create_session_factory
from grounded_answers.models import Document, TenantMembership, User
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction
from tests.db.database import DbHandle
from tests.db.seed import Seeded

pytestmark = pytest.mark.db


def app_url(handle: DbHandle) -> str:
    """The app's login as a SQLAlchemy URL."""
    return handle.url_for("app_user", handle.passwords["app_user"]).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )


async def app_engine(handle: DbHandle) -> AsyncEngine:
    return create_async_engine(app_url(handle), pool_size=2, pool_pre_ping=True)


def record_statements(engine: AsyncEngine) -> Generator[list[str], None, None]:
    """Collect the SQL text of every statement sent on this engine until the generator closes."""
    statements: list[str] = []

    def _record(conn: Any, cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


def _one_value(handle: DbHandle, sql: str, user_id: Any) -> Any:
    """Read one value as the cluster admin. Test assertions only."""
    with psycopg.connect(handle.admin_test_url()) as conn:
        row = conn.execute(sql, (user_id,)).fetchone()
    assert row is not None, "expected one row"
    return row[0]


def membership_id(handle: DbHandle, user_id: Any) -> Any:
    return _one_value(
        handle, "SELECT id FROM grounded.tenant_memberships WHERE user_id = %s", user_id
    )


def membership_active(handle: DbHandle, user_id: Any) -> bool:
    return bool(
        _one_value(
            handle, "SELECT active FROM grounded.tenant_memberships WHERE user_id = %s", user_id
        )
    )


@pytest.mark.anyio
async def test_unloaded_relationship_raises_and_sends_no_query(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    context = TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
    engine = await app_engine(grounded_db)
    try:
        factory = create_session_factory(engine)
        recorder = record_statements(engine)
        statements = next(recorder)
        async with begin_tenant_transaction(factory, context) as session:
            membership = (
                await session.execute(
                    select(TenantMembership).where(TenantMembership.user_id == seeded.a.admin)
                )
            ).scalar_one()
            before = len(statements)
            with pytest.raises(InvalidRequestError, match="lazy"):
                _ = membership.user
            assert len(statements) == before, "lazy='raise' must not send a query"
        recorder.close()
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_select_user_as_app_user_works_without_password_hash(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    context = TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
    engine = await app_engine(grounded_db)
    try:
        factory = create_session_factory(engine)
        recorder = record_statements(engine)
        statements = next(recorder)
        async with begin_tenant_transaction(factory, context) as session:
            statements.clear()
            users = (await session.execute(select(User))).scalars().all()
            select_sql = statements[-1]
        recorder.close()
    finally:
        await engine.dispose()

    ids = {user.id for user in users}
    assert seeded.a.admin in ids
    assert seeded.a.employee in ids
    assert seeded.b.admin not in ids, "RLS: another tenant's users are not visible"
    assert "password_hash" not in select_sql
    assert "updated_at" not in select_sql


@pytest.mark.anyio
async def test_loading_password_hash_explicitly_is_refused(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    context = TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
    engine = await app_engine(grounded_db)
    try:
        factory = create_session_factory(engine)
        with pytest.raises(DBAPIError) as refused:
            async with begin_tenant_transaction(factory, context) as session:
                await session.execute(select(User.password_hash))
    finally:
        await engine.dispose()

    assert isinstance(refused.value.orig, psycopg.errors.InsufficientPrivilege)


@pytest.mark.anyio
async def test_insert_returns_server_defaults_without_a_lazy_load(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    """eager_defaults: created_at is readable straight after the flush, with no extra query."""
    context = TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
    engine = await app_engine(grounded_db)
    try:
        factory = create_session_factory(engine)
        async with begin_tenant_transaction(factory, context) as session:
            document = Document(tenant_id=seeded.a.tenant, title="ORM default check")
            session.add(document)
            await session.flush()
            created = document.created_at
    finally:
        await engine.dispose()
    assert created.tzinfo is not None


@pytest.mark.anyio
async def test_rls_hides_another_tenants_membership_from_an_update(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    """Sanity for the rowcount tests: the update matches no row, and PostgreSQL raises no error."""
    context = TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
    other_membership = membership_id(grounded_db, seeded.b.admin)
    engine = await app_engine(grounded_db)
    try:
        factory = create_session_factory(engine)
        async with begin_tenant_transaction(factory, context) as session:
            result = await session.execute(
                text("UPDATE grounded.tenant_memberships SET active = false WHERE id = :id"),
                {"id": other_membership},
            )
            assert isinstance(result, CursorResult)
            assert result.rowcount == 0
    finally:
        await engine.dispose()
    assert membership_active(grounded_db, seeded.b.admin) is True

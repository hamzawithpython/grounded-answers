"""
begin_worker_transaction (ADR 0017): the same rules as the tenant helper, tenant only, no user.

- The tenant is set, and the user stays unset, inside the transaction.
- Rollback and error leave no context on the same pooled connection (the proof is the backend pid).
- Under a worker's tenant context, RLS shows only that tenant's documents.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from grounded_answers.tenant_db import WorkerContext, begin_worker_transaction

from .database import DbHandle
from .seed import Seeded

pytestmark = pytest.mark.db

CONTEXT_SQL = text(
    "SELECT NULLIF(current_setting('app.current_tenant', true), ''), "
    "NULLIF(current_setting('app.current_user', true), '')"
)


def _worker_url(handle: DbHandle) -> str:
    return handle.url_for("worker_user", handle.passwords["worker_user"]).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )


@asynccontextmanager
async def _one_connection(handle: DbHandle) -> AsyncIterator[AsyncEngine]:
    engine: AsyncEngine = create_async_engine(
        _worker_url(handle), pool_size=1, max_overflow=0, pool_pre_ping=True
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _bare_context(factory: async_sessionmaker[AsyncSession]) -> tuple[Any, Any]:
    """A transaction that sets nothing. This is what the next job would see on the connection."""
    async with factory() as session, session.begin():
        row = (await session.execute(CONTEXT_SQL)).one()
    return row[0], row[1]


@pytest.mark.anyio
async def test_worker_transaction_sets_the_tenant_and_leaves_the_user_unset(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    async with _one_connection(grounded_db) as engine:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with begin_worker_transaction(
            factory, WorkerContext(tenant_id=seeded.a.tenant)
        ) as session:
            tenant, user = (await session.execute(CONTEXT_SQL)).one()
    assert tenant == str(seeded.a.tenant)
    assert user is None


@pytest.mark.anyio
async def test_worker_context_does_not_survive_rollback_on_the_same_connection(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    async with _one_connection(grounded_db) as engine:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        with pytest.raises(RuntimeError, match="job failed"):
            async with begin_worker_transaction(
                factory, WorkerContext(tenant_id=seeded.b.tenant)
            ) as session:
                await session.execute(text("SELECT 1"))
                raise RuntimeError("job failed")
        assert await _bare_context(factory) == (None, None)


@pytest.mark.anyio
async def test_worker_context_does_not_survive_commit_on_the_same_connection(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    async with _one_connection(grounded_db) as engine:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with begin_worker_transaction(
            factory, WorkerContext(tenant_id=seeded.b.tenant)
        ) as session:
            await session.execute(text("SELECT 1"))
        assert await _bare_context(factory) == (None, None)


@pytest.mark.anyio
async def test_worker_sees_only_its_own_tenants_documents(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    async with _one_connection(grounded_db) as engine:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with begin_worker_transaction(
            factory, WorkerContext(tenant_id=seeded.a.tenant)
        ) as session:
            visible = set(
                (await session.execute(text("SELECT id FROM grounded.documents"))).scalars()
            )
    assert seeded.a.document in visible
    assert seeded.b.document not in visible

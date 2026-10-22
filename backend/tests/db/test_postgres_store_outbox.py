"""
PostgresObjectStore inside the real transaction (ADR 0018, outbox guarantee), and the rowcount rule.

The fake cannot prove these, because it has no transactions: a rollback of the caller's transaction
must remove the bytes, and only PostgreSQL can show that.
"""

import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from grounded_answers.database import create_session_factory
from grounded_answers.rowcount import RowcountMismatchError, expect_rowcount_in
from grounded_answers.storage.postgres_store import PostgresObjectStore
from grounded_answers.tenant_db import (
    TenantContext,
    WorkerContext,
    begin_tenant_transaction,
    begin_worker_transaction,
)

from .database import DbHandle
from .seed import Seeded

pytestmark = pytest.mark.db


def _app_factory(handle: DbHandle) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    url = handle.url_for("app_user", handle.passwords["app_user"]).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    engine = create_async_engine(url, pool_pre_ping=True)
    return engine, create_session_factory(engine)


def _queued_version(handle: DbHandle, seeded: Seeded) -> uuid.UUID:
    """A queued version, committed by the admin, so the app can attach bytes to it."""
    document, version = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(handle.admin_test_url(), autocommit=True) as admin:
        admin.execute(
            "INSERT INTO grounded.documents (id, tenant_id, title) VALUES (%s, %s, 'Outbox')",
            (document, seeded.a.tenant),
        )
        admin.execute(
            """
            INSERT INTO grounded.document_versions (
              id, tenant_id, document_id, version_no, original_filename, content_type,
              size_bytes, sha256, storage_key, status, created_by
            ) VALUES (%s, %s, %s, 1, 'o.txt', 'text/plain', 16, %s, %s, 'queued', %s)
            """,
            (
                version,
                seeded.a.tenant,
                document,
                secrets.token_bytes(32),
                f"tenant/{seeded.a.tenant}/{uuid.uuid4()}",
                seeded.a.admin,
            ),
        )
    return version


def _cleanup(handle: DbHandle, version: uuid.UUID) -> None:
    """Bytes, then the version, then its document (each references the one before it)."""
    with psycopg.connect(handle.admin_test_url(), autocommit=True) as admin:
        row = admin.execute(
            "SELECT document_id FROM grounded.document_versions WHERE id = %s", (version,)
        ).fetchone()
        admin.execute("DELETE FROM grounded.document_blobs WHERE version_id = %s", (version,))
        admin.execute("DELETE FROM grounded.document_versions WHERE id = %s", (version,))
        if row is not None:
            admin.execute("DELETE FROM grounded.documents WHERE id = %s", (row[0],))


def _blob_rows(handle: DbHandle, version: uuid.UUID) -> int:
    with psycopg.connect(handle.admin_test_url()) as admin:
        row = admin.execute(
            "SELECT count(*) FROM grounded.document_blobs WHERE version_id = %s", (version,)
        ).fetchone()
    assert row is not None
    return int(row[0])


@asynccontextmanager
async def _version_for_test(handle: DbHandle, seeded: Seeded) -> AsyncIterator[uuid.UUID]:
    version = _queued_version(handle, seeded)
    try:
        yield version
    finally:
        _cleanup(handle, version)


@pytest.mark.anyio
async def test_bytes_written_in_a_rolled_back_transaction_are_not_kept(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    """The outbox guarantee: a rolled-back upload leaves no bytes, as it leaves no version or job."""
    store = PostgresObjectStore()
    engine, factory = _app_factory(grounded_db)
    try:
        async with _version_for_test(grounded_db, seeded) as version:
            with pytest.raises(RuntimeError, match="upload failed after the bytes"):
                async with begin_tenant_transaction(
                    factory,
                    TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin),
                ) as session:
                    await store.put(
                        session, tenant_id=seeded.a.tenant, version_id=version, data=b"upload"
                    )
                    raise RuntimeError("upload failed after the bytes")
            assert _blob_rows(grounded_db, version) == 0
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_bytes_written_in_a_committed_transaction_are_kept(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    store = PostgresObjectStore()
    engine, factory = _app_factory(grounded_db)
    try:
        async with _version_for_test(grounded_db, seeded) as version:
            async with begin_tenant_transaction(
                factory,
                TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin),
            ) as session:
                await store.put(
                    session, tenant_id=seeded.a.tenant, version_id=version, data=b"kept"
                )
            assert _blob_rows(grounded_db, version) == 1
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_a_worker_transaction_reads_bytes_of_its_own_tenant_only(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    """The worker's context is the job's tenant, so the store reads that tenant's bytes only."""
    store = PostgresObjectStore()
    engine, factory = _app_factory(grounded_db)
    try:
        async with _version_for_test(grounded_db, seeded) as version:
            async with begin_tenant_transaction(
                factory,
                TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin),
            ) as session:
                await store.put(session, tenant_id=seeded.a.tenant, version_id=version, data=b"own")
            worker_url = grounded_db.url_for("worker_user", grounded_db.passwords["worker_user"])
            worker_engine = create_async_engine(
                worker_url.replace("postgresql://", "postgresql+psycopg://", 1)
            )
            try:
                worker_factory = create_session_factory(worker_engine)
                async with begin_worker_transaction(
                    worker_factory,
                    WorkerContext(tenant_id=seeded.b.tenant),
                ) as session:
                    assert not await store.exists(
                        session, tenant_id=seeded.b.tenant, version_id=version
                    )
            finally:
                await worker_engine.dispose()
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_expect_rowcount_in_accepts_the_allowed_counts_and_refuses_the_rest(
    grounded_db: DbHandle,
) -> None:
    """Zero and one are the only counts a purge delete may report (ADR 0018, rowcount rule)."""
    url = grounded_db.admin_test_url().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                text("DELETE FROM grounded.document_blobs WHERE version_id = :version"),
                {"version": uuid.uuid4()},
            )
            expect_rowcount_in(result, frozenset({0, 1}), "test.delete")
        async with engine.begin() as connection:
            result = await connection.execute(
                text("DELETE FROM grounded.document_blobs WHERE version_id = :version"),
                {"version": uuid.uuid4()},
            )
            with pytest.raises(RowcountMismatchError):
                expect_rowcount_in(result, frozenset({1}), "test.delete")
    finally:
        await engine.dispose()

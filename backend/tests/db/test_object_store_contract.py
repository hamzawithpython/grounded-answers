"""
The ObjectStore contract (ADR 0018): the same cases run against PostgresObjectStore and against
InMemoryObjectStore. If the fake and the real store ever disagree, one of these cases fails.

Each case is written once, in terms of a harness. The harness hides the backend:
- postgres: rows in document_blobs and document_versions, written by the admin (setup), and the
  store called inside the real tenant (app_user) or worker (worker_user) transaction;
- memory: the fake, with versions registered on it.

Setup rows are committed for the postgres backend (other roles must see them), and each one is
removed at the end of its test.
"""

import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from grounded_answers.storage.memory_store import InMemoryObjectStore
from grounded_answers.storage.object_store import (
    MAX_OBJECT_BYTES,
    InvalidObject,
    ObjectNotFound,
    ObjectRefused,
    ObjectStore,
)
from grounded_answers.storage.postgres_store import PostgresObjectStore
from grounded_answers.tenant_db import (
    TenantContext,
    WorkerContext,
    begin_tenant_transaction,
    begin_worker_transaction,
)

from .database import DbHandle
from .seed import Seeded, TenantSeed

pytestmark = pytest.mark.db

KINDS = ("memory", "postgres")


@dataclass
class _Created:
    documents: list[uuid.UUID] = field(default_factory=list)
    versions: list[uuid.UUID] = field(default_factory=list)


class _MemoryHarness:
    def __init__(self) -> None:
        self._fake = InMemoryObjectStore()
        self.store: ObjectStore = self._fake

    def make_version(self, tenant: TenantSeed, status: str) -> uuid.UUID:
        version = uuid.uuid4()
        self._fake.register_version(tenant_id=tenant.tenant, version_id=version, status=status)
        return version

    def bytes_present(self, tenant: TenantSeed, version: uuid.UUID) -> bool:
        return self._fake.has_bytes(tenant_id=tenant.tenant, version_id=version)

    def set_status(self, tenant: TenantSeed, version: uuid.UUID, status: str) -> None:
        self._fake.register_version(tenant_id=tenant.tenant, version_id=version, status=status)

    def forget_version(self, tenant: TenantSeed, version: uuid.UUID) -> None:
        self._fake.forget_version(tenant_id=tenant.tenant, version_id=version)

    @asynccontextmanager
    async def session(self, role: str, tenant: TenantSeed) -> AsyncIterator[None]:
        yield None


class _PostgresHarness:
    def __init__(
        self,
        admin: psycopg.Connection,
        app_factory: async_sessionmaker[AsyncSession],
        worker_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.store: ObjectStore = PostgresObjectStore()
        self._admin = admin
        self._app = app_factory
        self._worker = worker_factory
        self._created = _Created()

    def make_version(self, tenant: TenantSeed, status: str) -> uuid.UUID:
        document = uuid.uuid4()
        self._admin.execute(
            "INSERT INTO grounded.documents (id, tenant_id, title) VALUES (%s, %s, 'Contract')",
            (document, tenant.tenant),
        )
        self._created.documents.append(document)
        version = uuid.uuid4()
        self._admin.execute(
            """
            INSERT INTO grounded.document_versions (
              id, tenant_id, document_id, version_no, original_filename, content_type,
              size_bytes, sha256, storage_key, status, created_by, retired_at, purge_after
            ) VALUES (%s, %s, %s, 1, 'c.txt', 'text/plain', 16, %s, %s, %s, %s, %s, %s)
            """,
            (
                version,
                tenant.tenant,
                document,
                secrets.token_bytes(32),
                f"tenant/{tenant.tenant}/{uuid.uuid4()}",
                *self._status_columns(status, tenant),
            ),
        )
        self._created.versions.append(version)
        return version

    def set_status(self, tenant: TenantSeed, version: uuid.UUID, status: str) -> None:
        self._admin.execute(
            "UPDATE grounded.document_versions SET status = %s, retired_at = %s, purge_after = %s "
            "WHERE id = %s",
            (status, *self._retired_columns(status), version),
        )

    def bytes_present(self, tenant: TenantSeed, version: uuid.UUID) -> bool:
        row = self._admin.execute(
            "SELECT count(*) FROM grounded.document_blobs WHERE tenant_id = %s AND version_id = %s",
            (tenant.tenant, version),
        ).fetchone()
        assert row is not None
        return int(row[0]) == 1

    def forget_version(self, tenant: TenantSeed, version: uuid.UUID) -> None:
        try:
            self._admin.execute("DELETE FROM grounded.document_versions WHERE id = %s", (version,))
        except psycopg.errors.ForeignKeyViolation:
            raise ObjectRefused("the version still has bytes") from None

    @asynccontextmanager
    async def session(self, role: str, tenant: TenantSeed) -> AsyncIterator[AsyncSession]:
        if role == "app":
            context = TenantContext(tenant_id=tenant.tenant, user_id=tenant.admin)
            async with begin_tenant_transaction(self._app, context) as session:
                yield session
        else:
            async with begin_worker_transaction(
                self._worker, WorkerContext(tenant_id=tenant.tenant)
            ) as session:
                yield session

    def cleanup(self) -> None:
        for version in reversed(self._created.versions):
            self._admin.execute(
                "DELETE FROM grounded.document_blobs WHERE version_id = %s", (version,)
            )
            self._admin.execute("DELETE FROM grounded.document_versions WHERE id = %s", (version,))
        for document in reversed(self._created.documents):
            self._admin.execute("DELETE FROM grounded.documents WHERE id = %s", (document,))

    @staticmethod
    def _retired_columns(status: str) -> tuple[datetime | None, datetime | None]:
        if status in ("retired", "purging"):
            now = datetime.now(UTC)
            return now, now + timedelta(days=30)
        return None, None

    @classmethod
    def _status_columns(cls, status: str, tenant: TenantSeed) -> tuple[Any, ...]:
        retired_at, purge_after = cls._retired_columns(status)
        return (status, tenant.admin, retired_at, purge_after)


@asynccontextmanager
async def _open(kind: str, handle: DbHandle, seeded: Seeded) -> AsyncIterator[Any]:
    if kind == "memory":
        yield _MemoryHarness()
        return
    admin = psycopg.connect(handle.admin_test_url(), autocommit=True)
    engines: list[AsyncEngine] = []

    def factory(role: str) -> async_sessionmaker[AsyncSession]:
        url = handle.url_for(role, handle.passwords[role]).replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
        engine = create_async_engine(url, pool_pre_ping=True)
        engines.append(engine)
        return async_sessionmaker(engine, expire_on_commit=False)

    harness = _PostgresHarness(admin, factory("app_user"), factory("worker_user"))
    try:
        yield harness
    finally:
        harness.cleanup()
        for engine in engines:
            await engine.dispose()
        admin.close()


# ---- the cases ----------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("kind", KINDS)
async def test_put_then_get_returns_the_same_bytes(
    grounded_db: DbHandle, seeded: Seeded, kind: str
) -> None:
    async with _open(kind, grounded_db, seeded) as h:
        version = h.make_version(seeded.a, "queued")
        async with h.session("app", seeded.a) as session:
            await h.store.put(
                session, tenant_id=seeded.a.tenant, version_id=version, data=b"contract"
            )
        async with h.session("worker", seeded.a) as session:
            assert await h.store.get(session, tenant_id=seeded.a.tenant, version_id=version) == (
                b"contract"
            )


@pytest.mark.anyio
@pytest.mark.parametrize("kind", KINDS)
async def test_get_of_a_version_without_bytes_is_not_found(
    grounded_db: DbHandle, seeded: Seeded, kind: str
) -> None:
    async with _open(kind, grounded_db, seeded) as h:
        version = h.make_version(seeded.a, "queued")
        async with h.session("worker", seeded.a) as session:
            with pytest.raises(ObjectNotFound):
                await h.store.get(session, tenant_id=seeded.a.tenant, version_id=version)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", KINDS)
async def test_exists_reports_only_visible_bytes(
    grounded_db: DbHandle, seeded: Seeded, kind: str
) -> None:
    async with _open(kind, grounded_db, seeded) as h:
        version = h.make_version(seeded.a, "queued")
        async with h.session("worker", seeded.a) as session:
            assert not await h.store.exists(session, tenant_id=seeded.a.tenant, version_id=version)
        async with h.session("app", seeded.a) as session:
            await h.store.put(session, tenant_id=seeded.a.tenant, version_id=version, data=b"x")
        async with h.session("worker", seeded.a) as session:
            assert await h.store.exists(session, tenant_id=seeded.a.tenant, version_id=version)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "data",
    [b"", b"x" * (MAX_OBJECT_BYTES + 1)],
    ids=["empty", "over-10-mib"],
)
async def test_empty_or_oversized_bytes_are_refused_before_any_write(
    grounded_db: DbHandle, seeded: Seeded, kind: str, data: bytes
) -> None:
    async with _open(kind, grounded_db, seeded) as h:
        version = h.make_version(seeded.a, "queued")
        with pytest.raises(InvalidObject):
            async with h.session("app", seeded.a) as session:
                await h.store.put(session, tenant_id=seeded.a.tenant, version_id=version, data=data)
        async with h.session("worker", seeded.a) as session:
            assert not await h.store.exists(session, tenant_id=seeded.a.tenant, version_id=version)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("status", ["ready", "processing", "failed", "retired", "purging"])
async def test_bytes_are_stored_only_for_a_queued_version(
    grounded_db: DbHandle, seeded: Seeded, kind: str, status: str
) -> None:
    async with _open(kind, grounded_db, seeded) as h:
        version = h.make_version(seeded.a, status)
        with pytest.raises(ObjectRefused):
            async with h.session("app", seeded.a) as session:
                await h.store.put(session, tenant_id=seeded.a.tenant, version_id=version, data=b"x")


@pytest.mark.anyio
@pytest.mark.parametrize("kind", KINDS)
async def test_bytes_cannot_be_stored_for_another_tenants_version(
    grounded_db: DbHandle, seeded: Seeded, kind: str
) -> None:
    async with _open(kind, grounded_db, seeded) as h:
        version_of_b = h.make_version(seeded.b, "queued")
        with pytest.raises(ObjectRefused):
            async with h.session("app", seeded.a) as session:
                await h.store.put(
                    session, tenant_id=seeded.a.tenant, version_id=version_of_b, data=b"x"
                )


@pytest.mark.anyio
@pytest.mark.parametrize("kind", KINDS)
async def test_tenant_b_cannot_read_tenant_a_bytes(
    grounded_db: DbHandle, seeded: Seeded, kind: str
) -> None:
    async with _open(kind, grounded_db, seeded) as h:
        version = h.make_version(seeded.a, "queued")
        async with h.session("app", seeded.a) as session:
            await h.store.put(
                session, tenant_id=seeded.a.tenant, version_id=version, data=b"secret"
            )
        async with h.session("worker", seeded.b) as session:
            with pytest.raises(ObjectNotFound):
                await h.store.get(session, tenant_id=seeded.b.tenant, version_id=version)
            assert not await h.store.exists(session, tenant_id=seeded.b.tenant, version_id=version)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", KINDS)
async def test_delete_is_idempotent_for_a_purging_version(
    grounded_db: DbHandle, seeded: Seeded, kind: str
) -> None:
    async with _open(kind, grounded_db, seeded) as h:
        version = h.make_version(seeded.a, "queued")
        async with h.session("app", seeded.a) as session:
            await h.store.put(session, tenant_id=seeded.a.tenant, version_id=version, data=b"x")
        h.set_status(seeded.a, version, "purging")
        async with h.session("worker", seeded.a) as session:
            assert await h.store.delete(session, tenant_id=seeded.a.tenant, version_id=version) == 1
        async with h.session("worker", seeded.a) as session:
            assert await h.store.delete(session, tenant_id=seeded.a.tenant, version_id=version) == 0
            assert not await h.store.exists(session, tenant_id=seeded.a.tenant, version_id=version)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("status", ["queued", "processing", "ready", "retired"])
async def test_delete_is_refused_for_a_version_that_is_not_purging(
    grounded_db: DbHandle, seeded: Seeded, kind: str, status: str
) -> None:
    """A 0-row delete must never read as success: the bytes would stay with nothing to say so."""
    async with _open(kind, grounded_db, seeded) as h:
        version = h.make_version(seeded.a, "queued")
        async with h.session("app", seeded.a) as session:
            await h.store.put(session, tenant_id=seeded.a.tenant, version_id=version, data=b"x")
        h.set_status(seeded.a, version, status)
        with pytest.raises(ObjectRefused):
            async with h.session("worker", seeded.a) as session:
                await h.store.delete(session, tenant_id=seeded.a.tenant, version_id=version)
        assert h.bytes_present(seeded.a, version), "the refused delete must leave the bytes"


@pytest.mark.anyio
@pytest.mark.parametrize("kind", KINDS)
async def test_delete_after_the_version_is_gone_returns_zero(
    grounded_db: DbHandle, seeded: Seeded, kind: str
) -> None:
    async with _open(kind, grounded_db, seeded) as h:
        version = h.make_version(seeded.a, "queued")
        async with h.session("app", seeded.a) as session:
            await h.store.put(session, tenant_id=seeded.a.tenant, version_id=version, data=b"x")
        h.set_status(seeded.a, version, "purging")
        async with h.session("worker", seeded.a) as session:
            assert await h.store.delete(session, tenant_id=seeded.a.tenant, version_id=version) == 1
        h.forget_version(seeded.a, version)
        async with h.session("worker", seeded.a) as session:
            assert await h.store.delete(session, tenant_id=seeded.a.tenant, version_id=version) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_version_cannot_be_removed_while_its_bytes_exist(
    grounded_db: DbHandle, seeded: Seeded, kind: str
) -> None:
    """Purge order (ADR 0007): bytes first, then the version row. The foreign key enforces it."""
    async with _open(kind, grounded_db, seeded) as h:
        version = h.make_version(seeded.a, "queued")
        async with h.session("app", seeded.a) as session:
            await h.store.put(session, tenant_id=seeded.a.tenant, version_id=version, data=b"x")
        h.set_status(seeded.a, version, "purging")
        with pytest.raises(ObjectRefused):
            h.forget_version(seeded.a, version)
        async with h.session("worker", seeded.a) as session:
            assert await h.store.delete(session, tenant_id=seeded.a.tenant, version_id=version) == 1
        h.forget_version(seeded.a, version)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", KINDS)
async def test_tenant_b_cannot_delete_tenant_a_bytes(
    grounded_db: DbHandle, seeded: Seeded, kind: str
) -> None:
    """Another tenant's delete does nothing: it counts 0 and A's bytes stay."""
    async with _open(kind, grounded_db, seeded) as h:
        version = h.make_version(seeded.a, "queued")
        async with h.session("app", seeded.a) as session:
            await h.store.put(session, tenant_id=seeded.a.tenant, version_id=version, data=b"x")
        h.set_status(seeded.a, version, "purging")
        async with h.session("worker", seeded.b) as session:
            assert await h.store.delete(session, tenant_id=seeded.b.tenant, version_id=version) == 0
        async with h.session("worker", seeded.a) as session:
            assert await h.store.exists(session, tenant_id=seeded.a.tenant, version_id=version)

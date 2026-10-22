"""
The poison-job rule (ADR 0015 decision 9), end to end on the real worker.

A job carries a tenant id, and the worker treats it as untrusted. A job for document A that names
tenant B is refused: DocumentNotFoundForTenant, the job ends failed, the task body ran exactly
once (no retry), and the captured logs hold no tenant or document id. A correct job succeeds.

The task is test-only. It calls the production helper under begin_worker_transaction, the same
path a real ingestion task will use.
"""

import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import psycopg
import pytest
from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from grounded_answers.jobs import enqueue
from grounded_answers.jobs.app import build_app
from grounded_answers.jobs.checks import DocumentNotFoundForTenant, load_document_for_worker
from grounded_answers.jobs.enqueue import enqueue_job
from grounded_answers.jobs.logging_filter import RedactJobArguments
from grounded_answers.tenant_db import (
    TenantContext,
    WorkerContext,
    begin_tenant_transaction,
    begin_worker_transaction,
)

from .database import DbHandle
from .seed import Seeded

pytestmark = pytest.mark.db

TASK = "grounded.test_check_document"


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def collected() -> Iterator[_Collect]:
    """The production filter, on both job loggers, at INFO, for one test."""
    handler = _Collect()
    handler.addFilter(RedactJobArguments())
    loggers = [logging.getLogger("procrastinate"), logging.getLogger("grounded_answers.jobs")]
    previous = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        for logger, level in zip(loggers, previous, strict=True):
            logger.removeHandler(handler)
            logger.setLevel(level)


@pytest.fixture
def allowed(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(enqueue, "ALLOWED_TASKS", frozenset({TASK}))


def _url(handle: DbHandle, role: str) -> str:
    return handle.url_for(role, handle.passwords[role])


@asynccontextmanager
async def _worker_factory(handle: DbHandle) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = _url(handle, "worker_user").replace("postgresql://", "postgresql+psycopg://", 1)
    engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@asynccontextmanager
async def _app_factory(handle: DbHandle) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = _url(handle, "app_user").replace("postgresql://", "postgresql+psycopg://", 1)
    engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _enqueue_check(
    handle: DbHandle, seeded: Seeded, *, document: uuid.UUID, tenant: uuid.UUID
) -> int:
    """Enqueue one check job from tenant A's transaction, with the given (possibly wrong) ids."""
    app = build_app(_url(handle, "app_user"))
    async with _app_factory(handle) as factory:
        async with begin_tenant_transaction(
            factory, TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
        ) as session:
            return await enqueue_job(
                session, app, TASK, {"document_id": document, "tenant_id": tenant}
            )


def _job(handle: DbHandle, job_id: int) -> tuple[str, int]:
    with psycopg.connect(handle.admin_test_url()) as admin:
        row = admin.execute(
            "SELECT status, attempts FROM queue.procrastinate_jobs WHERE id = %s", (job_id,)
        ).fetchone()
    assert row is not None, "the job row must still exist after the worker ran it"
    return str(row[0]), int(row[1])


def _document_exists(handle: DbHandle, document_id: uuid.UUID) -> bool:
    with psycopg.connect(handle.admin_test_url()) as admin:
        row = admin.execute(
            "SELECT count(*) FROM grounded.documents WHERE id = %s", (document_id,)
        ).fetchone()
    assert row is not None
    return int(row[0]) == 1


def _texts(records: list[logging.LogRecord]) -> list[str]:
    return [f"{record.getMessage()} {vars(record)!r}" for record in records]


@pytest.mark.anyio
async def test_a_job_for_another_tenant_is_refused_once_and_logs_no_ids(
    grounded_db: DbHandle, seeded: Seeded, allowed: None, collected: _Collect
) -> None:
    document_id = seeded.a.document
    wrong_tenant = seeded.b.tenant
    job_id = await _enqueue_check(grounded_db, seeded, document=document_id, tenant=wrong_tenant)

    worker = build_app(_url(grounded_db, "worker_user"))
    calls: list[str] = []

    @worker.task(name=TASK)
    async def check(document_id: str, tenant_id: str) -> None:
        calls.append(document_id)
        async with _worker_factory(grounded_db) as factory:
            async with begin_worker_transaction(
                factory, WorkerContext(tenant_id=uuid.UUID(tenant_id))
            ) as session:
                await load_document_for_worker(session, uuid.UUID(document_id))

    async with worker.open_async():
        await worker.run_worker_async(wait=False, install_signal_handlers=False)

    assert calls == [str(document_id)], "the task body must run exactly once (no retry)"
    assert _job(grounded_db, job_id) == ("failed", 1)
    assert _document_exists(grounded_db, document_id), "the refused job must change nothing"

    texts = _texts(collected.records)
    assert any("ingest.refused reason=document_not_in_tenant" in text for text in texts)
    assert any("ended with status: Error" in text for text in texts), "the outcome is logged"
    for text in texts:
        assert str(document_id) not in text, "a log line holds the document id"
        assert str(wrong_tenant) not in text, "a log line holds the tenant id"


@pytest.mark.anyio
async def test_a_job_for_its_own_tenant_succeeds(
    grounded_db: DbHandle, seeded: Seeded, allowed: None, collected: _Collect
) -> None:
    document_id = seeded.a.document
    own_tenant = seeded.a.tenant
    job_id = await _enqueue_check(grounded_db, seeded, document=document_id, tenant=own_tenant)

    worker = build_app(_url(grounded_db, "worker_user"))
    calls: list[str] = []

    @worker.task(name=TASK)
    async def check(document_id: str, tenant_id: str) -> None:
        calls.append(document_id)
        async with _worker_factory(grounded_db) as factory:
            async with begin_worker_transaction(
                factory, WorkerContext(tenant_id=uuid.UUID(tenant_id))
            ) as session:
                await load_document_for_worker(session, uuid.UUID(document_id))

    async with worker.open_async():
        await worker.run_worker_async(wait=False, install_signal_handlers=False)

    assert calls == [str(document_id)]
    assert _job(grounded_db, job_id) == ("succeeded", 1)
    texts = _texts(collected.records)
    assert any("ended with status: Success" in text for text in texts)
    for text in texts:
        assert str(document_id) not in text
        assert str(own_tenant) not in text


@pytest.mark.anyio
async def test_the_helper_refuses_a_document_outside_the_tenant(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    """Called directly under tenant B's worker context, A's document is not found: refused."""
    async with _worker_factory(grounded_db) as factory:
        with pytest.raises(DocumentNotFoundForTenant):
            async with begin_worker_transaction(
                factory, WorkerContext(tenant_id=seeded.b.tenant)
            ) as session:
                await load_document_for_worker(session, seeded.a.document)


@pytest.mark.anyio
async def test_the_helper_finds_a_document_inside_the_tenant(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    async with _worker_factory(grounded_db) as factory:
        async with begin_worker_transaction(
            factory, WorkerContext(tenant_id=seeded.a.tenant)
        ) as session:
            found = await load_document_for_worker(session, seeded.a.document)
    assert found == seeded.a.document

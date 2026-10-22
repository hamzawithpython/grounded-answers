"""
enqueue_job on the tenant transaction (ADR 0015 outbox, ADR 0017).

The job row must exist exactly when the transaction commits. Rollback leaves no job row, so a
document never exists without its job, and a job never exists without its document.
"""

import io
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

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
from grounded_answers.jobs.enqueue import InvalidJobArgument, TaskNotAllowed, enqueue_job
from grounded_answers.jobs.logging_filter import install_job_log_redaction
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction

from .database import DbHandle
from .seed import Seeded

pytestmark = pytest.mark.db

TASK = "grounded.enqueue_check"


@pytest.fixture
def allowed(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(enqueue, "ALLOWED_TASKS", frozenset({TASK}))


def _app_url(handle: DbHandle) -> str:
    return handle.url_for("app_user", handle.passwords["app_user"])


def _sqlalchemy_app_url(handle: DbHandle) -> str:
    return _app_url(handle).replace("postgresql://", "postgresql+psycopg://", 1)


@asynccontextmanager
async def _factory(handle: DbHandle) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = create_async_engine(_sqlalchemy_app_url(handle), pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _jobs_with_document(handle: DbHandle, document_id: uuid.UUID) -> list[tuple[Any, ...]]:
    with psycopg.connect(handle.admin_test_url()) as admin:
        return admin.execute(
            "SELECT id, args, status FROM queue.procrastinate_jobs "
            "WHERE task_name = %s AND args->>'document_id' = %s",
            (TASK, str(document_id)),
        ).fetchall()


def _events_for(handle: DbHandle, job_id: int) -> int:
    with psycopg.connect(handle.admin_test_url()) as admin:
        row = admin.execute(
            "SELECT count(*) FROM queue.procrastinate_events WHERE job_id = %s", (job_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.anyio
async def test_a_committed_transaction_keeps_its_job(
    grounded_db: DbHandle, seeded: Seeded, allowed: None
) -> None:
    document_id = uuid.uuid4()
    app = build_app(_app_url(grounded_db))
    async with _factory(grounded_db) as factory:
        async with begin_tenant_transaction(
            factory, TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
        ) as session:
            await enqueue_job(session, app, TASK, {"document_id": document_id})

    rows = _jobs_with_document(grounded_db, document_id)
    assert len(rows) == 1, "a committed transaction keeps exactly one job"
    job_id, _args, status = rows[0]
    assert status == "todo"
    assert _events_for(grounded_db, job_id) == 1, "the deferred event is written with the job"


@pytest.mark.anyio
async def test_a_rolled_back_transaction_leaves_no_job_row(
    grounded_db: DbHandle, seeded: Seeded, allowed: None
) -> None:
    document_id = uuid.uuid4()
    app = build_app(_app_url(grounded_db))
    async with _factory(grounded_db) as factory:
        with pytest.raises(RuntimeError, match="write failed after enqueue"):
            async with begin_tenant_transaction(
                factory, TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
            ) as session:
                await enqueue_job(session, app, TASK, {"document_id": document_id})
                raise RuntimeError("write failed after enqueue")

    assert _jobs_with_document(grounded_db, document_id) == [], (
        "rollback must remove the job row: the outbox guarantee"
    )


@pytest.mark.anyio
async def test_the_stored_arguments_are_uuid_strings_only(
    grounded_db: DbHandle, seeded: Seeded, allowed: None
) -> None:
    document_id = uuid.uuid4()
    app = build_app(_app_url(grounded_db))
    async with _factory(grounded_db) as factory:
        async with begin_tenant_transaction(
            factory, TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
        ) as session:
            await enqueue_job(
                session,
                app,
                TASK,
                {"document_id": document_id, "tenant_id": seeded.a.tenant},
            )

    ((_, args, _),) = _jobs_with_document(grounded_db, document_id)
    stored = json.loads(json.dumps(args))
    assert set(stored) == {"document_id", "tenant_id"}
    for value in stored.values():
        assert str(uuid.UUID(value)) == value, "every stored argument is a canonical UUID string"


@pytest.mark.anyio
async def test_an_unknown_task_writes_nothing(grounded_db: DbHandle, seeded: Seeded) -> None:
    document_id = uuid.uuid4()
    app = build_app(_app_url(grounded_db))
    async with _factory(grounded_db) as factory:
        with pytest.raises(TaskNotAllowed):
            async with begin_tenant_transaction(
                factory, TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
            ) as session:
                await enqueue_job(
                    session, app, "grounded.not_on_the_list", {"document_id": document_id}
                )
    assert _jobs_with_document(grounded_db, document_id) == []


@pytest.mark.anyio
async def test_a_non_uuid_argument_writes_nothing(
    grounded_db: DbHandle, seeded: Seeded, allowed: None
) -> None:
    document_id = uuid.uuid4()
    app = build_app(_app_url(grounded_db))
    async with _factory(grounded_db) as factory:
        with pytest.raises(InvalidJobArgument):
            async with begin_tenant_transaction(
                factory, TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
            ) as session:
                await enqueue_job(
                    session, app, TASK, {"document_id": document_id, "title": "Policy"}
                )
    assert _jobs_with_document(grounded_db, document_id) == []


@pytest.mark.anyio
async def test_the_web_apps_enqueue_log_line_holds_no_ids(
    grounded_db: DbHandle, seeded: Seeded, allowed: None, monkeypatch: MonkeyPatch
) -> None:
    """The enqueue log ('Deferred 1 job') carries the arguments unless the filter removes them."""
    document_id = uuid.uuid4()
    tenant_id = seeded.a.tenant
    handler = install_job_log_redaction()
    assert isinstance(handler, logging.StreamHandler), "the job handler writes to a stream"
    buffer = io.StringIO()
    previous = handler.setStream(buffer)
    app = build_app(_app_url(grounded_db))
    try:
        async with _factory(grounded_db) as factory:
            async with begin_tenant_transaction(
                factory, TenantContext(tenant_id=tenant_id, user_id=seeded.a.admin)
            ) as session:
                await enqueue_job(
                    session, app, TASK, {"document_id": document_id, "tenant_id": tenant_id}
                )
    finally:
        handler.setStream(previous)

    logged = buffer.getvalue()
    assert "Deferred 1 job" in logged, "the enqueue outcome is still logged"
    assert str(document_id) not in logged
    assert str(tenant_id) not in logged

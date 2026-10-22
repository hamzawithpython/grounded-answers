"""
A job runs end to end: enqueued as app_user, run by the worker as worker_user (ADR 0015, ADR 0017).

The worker's logs keep the outcome of each job and never hold its arguments. The check reads every
attribute of every record, after the production filter has run, so an id in any field fails it.
Dummy tasks are registered on the worker's App only in these tests.
"""

import logging
from collections.abc import Iterator

import psycopg
import pytest

from grounded_answers.jobs.app import build_app
from grounded_answers.jobs.logging_filter import RedactJobArguments

from .database import DbHandle
from .seed import Seeded

pytestmark = pytest.mark.db


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def collected() -> Iterator[_Collect]:
    """Root handler with the production filter, at INFO, for one test."""
    root = logging.getLogger()
    previous_level = root.level
    handler = _Collect()
    handler.addFilter(RedactJobArguments())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def _url(handle: DbHandle, role: str) -> str:
    return handle.url_for(role, handle.passwords[role])


async def _enqueue(handle: DbHandle, task_name: str, **kwargs: str) -> int:
    app = build_app(_url(handle, "app_user"))
    async with app.open_async():
        return int(await app.configure_task(name=task_name).defer_async(**kwargs))


def _job(handle: DbHandle, job_id: int) -> tuple[str, int]:
    with psycopg.connect(handle.admin_test_url()) as conn:
        row = conn.execute(
            "SELECT status, attempts FROM queue.procrastinate_jobs WHERE id = %s", (job_id,)
        ).fetchone()
    assert row is not None, "the job row must still exist after the worker ran it"
    return str(row[0]), int(row[1])


def _all_text(records: list[logging.LogRecord]) -> list[str]:
    """Every message and every attribute of every record, as text."""
    return [f"{record.getMessage()} {vars(record)!r}" for record in records]


@pytest.mark.anyio
async def test_a_failing_job_is_logged_as_an_error_without_its_ids(
    grounded_db: DbHandle, seeded: Seeded, collected: _Collect
) -> None:
    document_id = str(seeded.a.document)
    tenant_id = str(seeded.a.tenant)
    job_id = await _enqueue(
        grounded_db, "grounded.test_fail", document_id=document_id, tenant_id=tenant_id
    )

    worker = build_app(_url(grounded_db, "worker_user"))
    ran: list[str] = []

    @worker.task(name="grounded.test_fail")
    def fail(document_id: str, tenant_id: str) -> None:
        ran.append(document_id)
        raise RuntimeError("test failure")

    async with worker.open_async():
        await worker.run_worker_async(wait=False, install_signal_handlers=False)

    # attempts counts runs: Procrastinate's finish adds one. One run, and no retry (ADR 0015).
    assert ran == [document_id], "the failing task must run exactly once"
    assert _job(grounded_db, job_id) == ("failed", 1)

    texts = _all_text(collected.records)
    assert any("ended with status: Error" in text for text in texts), "the outcome is logged"
    for text in texts:
        assert document_id not in text, "a log line holds the document id"
        assert tenant_id not in text, "a log line holds the tenant id"


@pytest.mark.anyio
async def test_a_succeeding_job_is_logged_as_a_success_without_its_ids(
    grounded_db: DbHandle, seeded: Seeded, collected: _Collect
) -> None:
    document_id = str(seeded.a.document)
    tenant_id = str(seeded.a.tenant)
    job_id = await _enqueue(
        grounded_db, "grounded.test_ok", document_id=document_id, tenant_id=tenant_id
    )

    worker = build_app(_url(grounded_db, "worker_user"))
    ran: list[str] = []

    @worker.task(name="grounded.test_ok")
    def ok(document_id: str, tenant_id: str) -> None:
        ran.append(document_id)

    async with worker.open_async():
        await worker.run_worker_async(wait=False, install_signal_handlers=False)

    assert ran == [document_id]
    assert _job(grounded_db, job_id) == ("succeeded", 1)

    texts = _all_text(collected.records)
    assert any("ended with status: Success" in text for text in texts), "the outcome is logged"
    for text in texts:
        assert document_id not in text
        assert tenant_id not in text

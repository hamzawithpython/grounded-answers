"""The stalled-version retry (ADR 0023, as amended; migration 0011).

- The definer function defers only processing versions whose lease has expired, across tenants.
- It refuses a lease outside 120 to 7200 seconds.
- A version that already has a waiting ingest job is skipped, and the job already there is untouched. The other
  stalled versions are still deferred, so one duplicate never stops the sweep.
- Repeated sweeps never pile up jobs.
- worker_user can reach the enqueue only through the function, not through the queue tables or the defer function.
- A killed attempt is completed by the job the sweep deferred, exactly once.

Setup rows are removed afterwards, and so are the queue rows the test deferred.
"""

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import errors

from grounded_answers.ingest.pipeline import IngestJob, Outcome, run_ingestion
from grounded_answers.jobs.app import build_app
from grounded_answers.jobs.ingest_task import attempt_token
from grounded_answers.jobs.stalled import ingest_lock, sweep_stalled_versions
from grounded_answers.jobs.tasks import INGEST_DOCUMENT
from grounded_answers.storage.postgres_store import PostgresObjectStore

from .database import DbHandle
from .ingest_support import (
    FakeEmbedder,
    Made,
    age_lease,
    chunk_count,
    make_version,
    ready_audit_count,
    version_row,
    worker_sessions,
)
from .seed import Seeded

pytestmark = pytest.mark.db

MARKDOWN = "text/markdown"
POLICY_MD = b"# 3 Probation\n\n3.2 Probation is six months for LTV drivers.\n"


def _worker_conninfo(handle: DbHandle) -> str:
    return handle.url_for("worker_user", handle.passwords["worker_user"])


def _app_user_conninfo(handle: DbHandle) -> str:
    return handle.url_for("app_user", handle.passwords["app_user"])


def _worker(handle: DbHandle) -> psycopg.Connection:
    return psycopg.connect(_worker_conninfo(handle))


@pytest.fixture
def queued_locks(admin: psycopg.Connection) -> Iterator[list[str]]:
    """Queueing locks the test defers under. Their queue rows are removed afterwards."""
    locks: list[str] = []
    yield locks
    # The queue's delete trigger names its table without a schema, so the cleanup needs the queue schema on its path.
    admin.execute("SET search_path TO queue, public")
    for lock in locks:
        admin.execute(
            "DELETE FROM queue.procrastinate_events WHERE job_id IN "
            "(SELECT id FROM queue.procrastinate_jobs WHERE queueing_lock = %s)",
            (lock,),
        )
        admin.execute("DELETE FROM queue.procrastinate_jobs WHERE queueing_lock = %s", (lock,))


def _todo_jobs(admin: psycopg.Connection, lock: str) -> list[tuple[int, dict[str, str]]]:
    rows = admin.execute(
        "SELECT id, args FROM queue.procrastinate_jobs WHERE queueing_lock = %s AND status = 'todo'",
        (lock,),
    ).fetchall()
    return [(int(job_id), dict(args)) for job_id, args in rows]


def test_the_function_defers_only_expired_processing_versions(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    queued_locks: list[str],
) -> None:
    _, expired = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="processing",
    )
    age_lease(admin, expired)
    _, fresh = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="processing",
    )
    _, queued = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="queued",
    )
    _, other_tenant = make_version(
        admin,
        made,
        seeded.b.tenant,
        seeded.b.admin,
        content_type=MARKDOWN,
        data=None,
        status="processing",
    )
    age_lease(admin, other_tenant)
    for version in (expired, fresh, queued, other_tenant):
        queued_locks.append(ingest_lock(version))
    with _worker(grounded_db) as worker:
        worker.execute("SELECT * FROM grounded.enqueue_stalled_ingest(%s)", (120,))
        worker.commit()
    assert len(_todo_jobs(admin, ingest_lock(expired))) == 1
    assert len(_todo_jobs(admin, ingest_lock(other_tenant))) == 1, "the scan covers every tenant"
    assert _todo_jobs(admin, ingest_lock(fresh)) == []
    assert _todo_jobs(admin, ingest_lock(queued)) == []
    [(_job_id, args)] = _todo_jobs(admin, ingest_lock(other_tenant))
    assert args["tenant_id"] == str(seeded.b.tenant)


@pytest.mark.parametrize("seconds", [119, 7201])
def test_the_function_refuses_a_lease_outside_the_bounds(
    grounded_db: DbHandle, seconds: int
) -> None:
    with pytest.raises(errors.InvalidParameterValue, match="between 120 and 7200"):
        with _worker(grounded_db) as worker:
            worker.execute("SELECT * FROM grounded.enqueue_stalled_ingest(%s)", (seconds,))


def test_the_function_is_owned_by_the_definer_role_and_pins_its_search_path(
    admin: psycopg.Connection,
) -> None:
    row = admin.execute(
        "SELECT pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig "
        "FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'grounded' AND p.proname = 'enqueue_stalled_ingest'"
    ).fetchone()
    assert row is not None
    owner, security_definer, config = row
    assert owner == "retry_definer"
    assert security_definer is True
    assert any(str(item).startswith("search_path=") for item in config)


def test_worker_user_cannot_enqueue_except_through_the_function(grounded_db: DbHandle) -> None:
    """The function is the only enqueue path for worker_user: no direct insert, no defer function."""
    with _worker(grounded_db) as worker:
        with pytest.raises(errors.InsufficientPrivilege):
            worker.execute(
                "INSERT INTO queue.procrastinate_jobs (queue_name, task_name, args) "
                "VALUES ('default', 'grounded.ingest_document', '{}')"
            )
    with _worker(grounded_db) as worker:
        with pytest.raises(errors.InsufficientPrivilege):
            worker.execute(
                "SELECT queue.procrastinate_defer_jobs_v1(ARRAY[ROW('default', 'grounded.ingest_document', "
                "0, NULL, NULL, '{}'::jsonb, NULL)::queue.procrastinate_job_to_defer_v1])"
            )


@pytest.mark.anyio
async def test_a_version_with_a_waiting_job_is_skipped_and_its_job_is_untouched(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    queued_locks: list[str],
) -> None:
    """One stalled version already has a queued job. Another stalled version has none. The sweep defers exactly
    one new job, and the existing job is the same row, with the same arguments, afterwards."""
    _, waiting = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="processing",
    )
    age_lease(admin, waiting)
    _, without_job = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="processing",
    )
    age_lease(admin, without_job)
    waiting_lock = ingest_lock(waiting)
    free_lock = ingest_lock(without_job)
    queued_locks.extend([waiting_lock, free_lock])

    # The job that is already waiting, deferred the way the web app defers (app_user).
    app = build_app(_app_user_conninfo(grounded_db))
    async with app.open_async():
        await app.configure_task(name=INGEST_DOCUMENT, queueing_lock=waiting_lock).defer_async(
            tenant_id=str(seeded.a.tenant),
            document_id=str(_document_of(admin, waiting)),
            version_id=str(waiting),
        )
    [(existing_id, existing_args)] = _todo_jobs(admin, waiting_lock)

    async with worker_sessions(grounded_db) as factory:
        enqueued, already_waiting = await sweep_stalled_versions(
            session_factory=factory, lease_seconds=120
        )

    assert already_waiting >= 1, (
        "the version with a waiting job must be counted, not deferred again"
    )
    assert enqueued >= 1, (
        "the version without a job must still be deferred: one duplicate stops nothing"
    )
    assert _todo_jobs(admin, waiting_lock) == [(existing_id, existing_args)], (
        "the existing job is untouched"
    )
    assert len(_todo_jobs(admin, free_lock)) == 1


@pytest.mark.anyio
async def test_repeated_sweeps_never_pile_up_jobs(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    queued_locks: list[str],
) -> None:
    _, version = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="processing",
    )
    age_lease(admin, version)
    lock = ingest_lock(version)
    queued_locks.append(lock)
    async with worker_sessions(grounded_db) as factory:
        await sweep_stalled_versions(session_factory=factory, lease_seconds=120)
        _enqueued, already_waiting = await sweep_stalled_versions(
            session_factory=factory, lease_seconds=120
        )
    assert already_waiting >= 1
    assert len(_todo_jobs(admin, lock)) == 1


@pytest.mark.anyio
async def test_a_killed_attempt_is_completed_by_the_deferred_job_exactly_once(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    queued_locks: list[str],
) -> None:
    document, version = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=POLICY_MD,
        status="processing",
    )
    age_lease(admin, version)
    lock = ingest_lock(version)
    queued_locks.append(lock)
    async with worker_sessions(grounded_db) as factory:
        await sweep_stalled_versions(session_factory=factory, lease_seconds=120)
        [(job_id, args)] = _todo_jobs(admin, lock)
        assert args == {
            "tenant_id": str(seeded.a.tenant),
            "document_id": str(document),
            "version_id": str(version),
        }
        # The job runs the ingest code with its own token, derived from its id (ADR 0022, D1).
        job = IngestJob(
            tenant_id=uuid.UUID(args["tenant_id"]),
            document_id=uuid.UUID(args["document_id"]),
            version_id=uuid.UUID(args["version_id"]),
            attempt_token=attempt_token(job_id),
            lease_seconds=120,
        )
        first = await run_ingestion(
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=FakeEmbedder(),
            job=job,
        )
        second = await run_ingestion(
            session_factory=factory,
            object_store=PostgresObjectStore(),
            embedder=FakeEmbedder(),
            job=job,
        )
    assert first is Outcome.READY
    assert second is not Outcome.READY, "a finished version must not be written again"
    assert version_row(admin, version) == ("ready", None)
    assert chunk_count(admin, version) >= 1
    assert ready_audit_count(admin, version) == 1


def _document_of(admin: psycopg.Connection, version: uuid.UUID) -> uuid.UUID:
    row = admin.execute(
        "SELECT document_id FROM grounded.document_versions WHERE id = %s", (version,)
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))

"""The purge (ADR 0027, ADR 0007; ARCHITECTURE 5.7), run against PostgreSQL through the worker's own transaction.

Due times are set by the test (purge_after in the past or the future), so nothing waits for a clock. The sweep is the
definer grounded.enqueue_purge_jobs, called as worker_user. The purge itself is purge.purge_item, called directly here
(the queue delivers the same call in production).
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import psycopg
import pytest
from pytest import MonkeyPatch

from grounded_answers import purge
from grounded_answers.embedding.pinned import MODEL_NAME, MODEL_REVISION
from grounded_answers.jobs.tasks import PURGE_ITEM
from grounded_answers.models import EMBEDDING_DIMENSIONS
from grounded_answers.purge import purge_item, sweep_purge

from .database import DbHandle
from .ingest_support import Made, make_version, worker_sessions
from .seed import Seeded

pytestmark = [pytest.mark.db, pytest.mark.anyio]

MARKDOWN = "text/markdown"
PAST = datetime(2020, 1, 1, tzinfo=UTC)
FUTURE = datetime(2999, 1, 1, tzinfo=UTC)


def _vector() -> str:
    return "[" + ",".join("0.1" for _ in range(EMBEDDING_DIMENSIONS)) + "]"


def _chunk(admin: psycopg.Connection, seeded: Seeded, version: uuid.UUID) -> None:
    admin.execute(
        "INSERT INTO grounded.chunks (tenant_id, version_id, position, body, embedding, embedding_model, "
        "embedding_model_version) VALUES (%s, %s, 0, 'secret body', %s::extensions.vector, %s, %s)",
        (seeded.a.tenant, version, _vector(), MODEL_NAME, MODEL_REVISION),
    )


def _count(admin: psycopg.Connection, sql: str, params: tuple[Any, ...]) -> int:
    row = admin.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


def _version_exists(admin: psycopg.Connection, version: uuid.UUID) -> bool:
    return (
        _count(admin, "SELECT count(*) FROM grounded.document_versions WHERE id = %s", (version,))
        == 1
    )


def _document_exists(admin: psycopg.Connection, document: uuid.UUID) -> bool:
    return _count(admin, "SELECT count(*) FROM grounded.documents WHERE id = %s", (document,)) == 1


def _set_purge_after(admin: psycopg.Connection, version: uuid.UUID, when: datetime) -> None:
    admin.execute(
        "UPDATE grounded.document_versions SET purge_after = %s WHERE id = %s", (when, version)
    )


def _retired_version(
    admin: psycopg.Connection,
    made: Made,
    seeded: Seeded,
    document: uuid.UUID,
    number: int,
    when: datetime,
) -> uuid.UUID:
    """A retired version of `document`, with one chunk and bytes, whose purge_after is `when`."""
    _, version = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=b"old bytes",
        status="retired",
        version_no=number,
    )
    admin.execute(
        "UPDATE grounded.document_versions SET document_id = %s WHERE id = %s", (document, version)
    )
    _set_purge_after(admin, version, when)
    _chunk(admin, seeded, version)
    return version


def _deleted_document(
    admin: psycopg.Connection, made: Made, seeded: Seeded, when: datetime
) -> uuid.UUID:
    """A deleted document with its purge_after at `when`, and no versions. The caller adds them."""
    document = uuid.uuid4()
    admin.execute(
        "INSERT INTO grounded.documents (id, tenant_id, title, deleted_at, purge_after) "
        "VALUES (%s, %s, %s, %s, %s)",
        (document, seeded.a.tenant, "Deleted", when - timedelta(days=30), when),
    )
    made.documents.append(document)
    return document


def _live_with_current_and_old(
    admin: psycopg.Connection, made: Made, seeded: Seeded
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A live document: a ready current version (with a chunk), and a retired old version due now."""
    document, current = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=b"current bytes",
        status="ready",
    )
    admin.execute(
        "UPDATE grounded.documents SET current_version_id = %s WHERE id = %s", (current, document)
    )
    _chunk(admin, seeded, current)
    old = _retired_version(admin, made, seeded, document, 2, PAST)
    return document, current, old


@pytest.fixture
def purge_queue(admin: psycopg.Connection) -> Iterator[list[str]]:
    """Locks the test deferred under. Their queue rows are removed afterwards (the queue's delete trigger needs the
    queue schema on the path)."""
    locks: list[str] = []
    yield locks
    admin.execute("SET search_path TO queue, grounded, public")
    for lock in locks:
        admin.execute(
            "DELETE FROM queue.procrastinate_events WHERE job_id IN "
            "(SELECT id FROM queue.procrastinate_jobs WHERE queueing_lock = %s)",
            (lock,),
        )
        admin.execute("DELETE FROM queue.procrastinate_jobs WHERE queueing_lock = %s", (lock,))


# ---- nothing before its time ----


async def test_nothing_is_purged_before_purge_after(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    """Before purge_after, every item is still there: the document, its version, the version's chunk and bytes, and
    a live document's retired version. The state is asserted after each job, directly. It is not inferred from an
    exception, so a job that deletes early fails here, on the rows."""
    document = _deleted_document(admin, made, seeded, FUTURE)
    version = _retired_version(admin, made, seeded, document, 1, FUTURE)
    live, current, old = _live_with_current_and_old(admin, made, seeded)
    _set_purge_after(admin, old, FUTURE)
    jobs: list[tuple[str, Literal["document", "version"], uuid.UUID]] = [
        ("the deleted document", "document", document),
        ("a version of the deleted document", "version", version),
        ("a retired version of a live document", "version", old),
    ]
    outcomes: dict[str, str] = {}
    async with worker_sessions(grounded_db) as factory:
        for label, kind, item in jobs:
            try:
                outcomes[label] = await purge_item(
                    session_factory=factory, tenant_id=seeded.a.tenant, kind=kind, item_id=item
                )
            except RuntimeError as exc:
                outcomes[label] = f"raised: {exc}"
            assert _document_exists(admin, document), (
                f"{label}: the deleted document was removed early"
            )
            assert _version_exists(admin, version), f"{label}: the version was removed early"
            assert (
                _count(
                    admin, "SELECT count(*) FROM grounded.chunks WHERE version_id = %s", (version,)
                )
                == 1
            ), f"{label}: the chunk was removed early"
            assert (
                _count(
                    admin,
                    "SELECT count(*) FROM grounded.document_blobs WHERE version_id = %s",
                    (version,),
                )
                == 1
            ), f"{label}: the bytes were removed early"
            assert _document_exists(admin, live) and _version_exists(admin, current), (
                f"{label}: the live document's current version was touched"
            )
            assert _version_exists(admin, old), f"{label}: the retired version was removed early"
            assert (
                _count(admin, "SELECT count(*) FROM grounded.chunks WHERE version_id = %s", (old,))
                == 1
            )
    assert outcomes == {
        "the deleted document": "not_due",
        "a version of the deleted document": "covered_by_document",
        "a retired version of a live document": "not_due",
    }


# ---- completeness, and a retired version of a live document ----


async def test_a_deleted_document_is_purged_completely(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document = _deleted_document(admin, made, seeded, PAST)
    first = _retired_version(admin, made, seeded, document, 1, PAST)
    second = _retired_version(admin, made, seeded, document, 2, PAST)
    async with worker_sessions(grounded_db) as factory:
        outcome = await purge_item(
            session_factory=factory, tenant_id=seeded.a.tenant, kind="document", item_id=document
        )

    assert outcome == "purged"
    assert not _document_exists(admin, document)
    assert not _version_exists(admin, first) and not _version_exists(admin, second)
    for version in (first, second):
        assert (
            _count(admin, "SELECT count(*) FROM grounded.chunks WHERE version_id = %s", (version,))
            == 0
        )
        assert (
            _count(
                admin,
                "SELECT count(*) FROM grounded.document_blobs WHERE version_id = %s",
                (version,),
            )
            == 0
        )


async def test_a_retired_version_of_a_live_document_is_purged_and_the_current_one_is_untouched(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document, current, old = _live_with_current_and_old(admin, made, seeded)
    async with worker_sessions(grounded_db) as factory:
        outcome = await purge_item(
            session_factory=factory, tenant_id=seeded.a.tenant, kind="version", item_id=old
        )

    assert outcome == "purged"
    assert not _version_exists(admin, old)
    assert _document_exists(admin, document)
    assert (
        _count(
            admin,
            "SELECT count(*) FROM grounded.documents WHERE id = %s AND current_version_id = %s",
            (document, current),
        )
        == 1
    )
    assert (
        _count(admin, "SELECT count(*) FROM grounded.chunks WHERE version_id = %s", (current,)) == 1
    )
    assert (
        _count(
            admin, "SELECT count(*) FROM grounded.document_blobs WHERE version_id = %s", (current,)
        )
        == 1
    )


async def test_a_version_of_a_deleted_document_is_left_to_its_document_job(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    """Its retention is the document's: the version job must not purge it, even when due (the document job removes it)."""
    document = _deleted_document(admin, made, seeded, PAST)
    version = _retired_version(admin, made, seeded, document, 1, PAST)
    async with worker_sessions(grounded_db) as factory:
        outcome = await purge_item(
            session_factory=factory, tenant_id=seeded.a.tenant, kind="version", item_id=version
        )
    assert outcome == "covered_by_document"
    assert _version_exists(admin, version)


# ---- the transaction: a crash rolls back, and a re-run completes ----


async def test_a_crash_rolls_the_item_back_and_a_rerun_completes(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    monkeypatch: MonkeyPatch,
) -> None:
    """The chunks and bytes are deleted, then the audit write fails, as if the worker died. Nothing may be left
    half-purged, and the re-run completes the purge."""
    document = _deleted_document(admin, made, seeded, PAST)
    version = _retired_version(admin, made, seeded, document, 1, PAST)

    async def crash(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the worker stopped before the audit row")

    async with worker_sessions(grounded_db) as factory:
        monkeypatch.setattr(purge, "_audit", crash)
        with pytest.raises(RuntimeError, match="worker stopped"):
            await purge_item(
                session_factory=factory,
                tenant_id=seeded.a.tenant,
                kind="document",
                item_id=document,
            )
        monkeypatch.undo()

        assert _document_exists(admin, document) and _version_exists(admin, version)
        assert (
            _count(admin, "SELECT count(*) FROM grounded.chunks WHERE version_id = %s", (version,))
            == 1
        )
        assert (
            _count(
                admin,
                "SELECT count(*) FROM grounded.document_blobs WHERE version_id = %s",
                (version,),
            )
            == 1
        )
        status = admin.execute(
            "SELECT status FROM grounded.document_versions WHERE id = %s", (version,)
        ).fetchone()
        assert status == ("retired",), "a rolled-back purge leaves the version retired, not purging"

        assert (
            await purge_item(
                session_factory=factory,
                tenant_id=seeded.a.tenant,
                kind="document",
                item_id=document,
            )
            == "purged"
        )
    assert not _document_exists(admin, document) and not _version_exists(admin, version)


async def test_rerunning_a_finished_purge_changes_nothing(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document = _deleted_document(admin, made, seeded, PAST)
    _retired_version(admin, made, seeded, document, 1, PAST)
    async with worker_sessions(grounded_db) as factory:
        assert (
            await purge_item(
                session_factory=factory,
                tenant_id=seeded.a.tenant,
                kind="document",
                item_id=document,
            )
            == "purged"
        )
        assert (
            await purge_item(
                session_factory=factory,
                tenant_id=seeded.a.tenant,
                kind="document",
                item_id=document,
            )
            == "gone"
        )
    audits = _count(
        admin,
        "SELECT count(*) FROM grounded.audit_log WHERE action = 'document.purged' AND target_id = %s",
        (document,),
    )
    assert audits == 1, "the second run writes no second audit row"


# ---- the audit row: counts only ----


async def test_the_audit_row_holds_counts_and_nothing_of_the_content(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document = _deleted_document(admin, made, seeded, PAST)
    version = _retired_version(admin, made, seeded, document, 1, PAST)
    title = admin.execute(
        "SELECT title FROM grounded.documents WHERE id = %s", (document,)
    ).fetchone()
    assert title is not None
    async with worker_sessions(grounded_db) as factory:
        await purge_item(
            session_factory=factory, tenant_id=seeded.a.tenant, kind="document", item_id=document
        )

    rows = admin.execute(
        "SELECT action, detail FROM grounded.audit_log WHERE target_id = %s AND action = 'document.purged'",
        (document,),
    ).fetchall()
    assert rows == [("document.purged", {"versions": 1, "chunks": 1, "blobs": 1})]
    assert "secret body" not in str(rows) and str(title[0]) not in str(rows)
    assert str(version) not in str(rows), "the audit row names the document, not its versions"


# ---- tenant scoping ----


async def test_a_purge_job_cannot_touch_another_tenants_item(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    """Tenant B's deleted document, offered to a job for tenant A: the session tenant hides it (RLS), so nothing changes."""
    document = uuid.uuid4()
    admin.execute(
        "INSERT INTO grounded.documents (id, tenant_id, title, deleted_at, purge_after) "
        "VALUES (%s, %s, 'B', %s, %s)",
        (document, seeded.b.tenant, PAST, PAST),
    )
    made.documents.append(document)
    async with worker_sessions(grounded_db) as factory:
        outcome = await purge_item(
            session_factory=factory, tenant_id=seeded.a.tenant, kind="document", item_id=document
        )
    assert outcome == "gone"
    assert _document_exists(admin, document)


# ---- the sweep: one job per due item, and no duplicates ----


async def test_the_sweep_defers_the_due_items_and_nothing_else(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    purge_queue: list[str],
) -> None:
    due_document = _deleted_document(admin, made, seeded, PAST)
    not_due_document = _deleted_document(admin, made, seeded, FUTURE)
    _document, current, old = _live_with_current_and_old(admin, made, seeded)
    for item in (due_document, not_due_document, old):
        purge_queue.extend([f"purge:document:{item}", f"purge:version:{item}"])
    async with worker_sessions(grounded_db) as factory:
        await sweep_purge(session_factory=factory)

    assert (
        _count(
            admin,
            "SELECT count(*) FROM queue.procrastinate_jobs WHERE queueing_lock = %s AND status = 'todo'",
            (f"purge:document:{due_document}",),
        )
        == 1
    )
    assert (
        _count(
            admin,
            "SELECT count(*) FROM queue.procrastinate_jobs WHERE queueing_lock = %s AND status = 'todo'",
            (f"purge:version:{old}",),
        )
        == 1
    )
    assert (
        _count(
            admin,
            "SELECT count(*) FROM queue.procrastinate_jobs WHERE queueing_lock = %s",
            (f"purge:document:{not_due_document}",),
        )
        == 0
    )
    assert (
        _count(
            admin,
            "SELECT count(*) FROM queue.procrastinate_jobs WHERE queueing_lock = %s",
            (f"purge:version:{current}",),
        )
        == 0
    )
    job = admin.execute(
        "SELECT task_name, args FROM queue.procrastinate_jobs WHERE queueing_lock = %s",
        (f"purge:version:{old}",),
    ).fetchone()
    assert job is not None and job[0] == PURGE_ITEM
    assert job[1] == {"tenant_id": str(seeded.a.tenant), "kind": "version", "item_id": str(old)}


async def test_repeated_sweeps_never_duplicate_a_purge_job(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    purge_queue: list[str],
) -> None:
    document = _deleted_document(admin, made, seeded, PAST)
    purge_queue.append(f"purge:document:{document}")
    async with worker_sessions(grounded_db) as factory:
        await sweep_purge(session_factory=factory)
        _enqueued, already_waiting = await sweep_purge(session_factory=factory)
    assert already_waiting >= 1
    assert (
        _count(
            admin,
            "SELECT count(*) FROM queue.procrastinate_jobs WHERE queueing_lock = %s AND status = 'todo'",
            (f"purge:document:{document}",),
        )
        == 1
    )


# ---- the retention setting: each row keeps the period it was retired with ----


def _ready_version(
    admin: psycopg.Connection, made: Made, seeded: Seeded, document: uuid.UUID, number: int
) -> uuid.UUID:
    _, version = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="ready",
        version_no=number,
    )
    admin.execute(
        "UPDATE grounded.document_versions SET document_id = %s WHERE id = %s", (document, version)
    )
    return version


def _swap_as_worker(
    handle: DbHandle, tenant: uuid.UUID, document: uuid.UUID, version: uuid.UUID, days: int
) -> None:
    with psycopg.connect(handle.url_for("worker_user", handle.passwords["worker_user"])) as worker:
        worker.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))
        worker.execute(
            "SELECT grounded.swap_current_version(%s, %s, %s)", (document, version, days)
        )
        worker.commit()


async def test_a_changed_retention_affects_only_later_retirements(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    """A version retired under 7 days keeps 7 days when a later retirement uses 90. Retention is written per row at
    retirement (ARCHITECTURE 5.5, ADR 0027)."""
    document, first = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="ready",
        version_no=1,
    )
    admin.execute(
        "UPDATE grounded.documents SET current_version_id = %s WHERE id = %s", (first, document)
    )
    second = _ready_version(admin, made, seeded, document, 2)
    third = _ready_version(admin, made, seeded, document, 3)

    _swap_as_worker(grounded_db, seeded.a.tenant, document, second, 7)  # first retired with 7 days
    _swap_as_worker(
        grounded_db, seeded.a.tenant, document, third, 90
    )  # second retired with 90 days

    def window(version: uuid.UUID) -> timedelta:
        row = admin.execute(
            "SELECT retired_at, purge_after FROM grounded.document_versions WHERE id = %s",
            (version,),
        ).fetchone()
        assert row is not None and row[0] is not None
        window: timedelta = row[1] - row[0]
        return window

    assert timedelta(days=6) < window(first) < timedelta(days=8), (
        "the earlier retirement keeps its 7 days"
    )
    assert timedelta(days=89) < window(second) < timedelta(days=91), (
        "the later retirement uses 90 days"
    )

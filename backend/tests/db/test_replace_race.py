"""Replacement and swap under concurrency (ADR 0024, migration 0013).

The document row is the serialization point. These tests hold it from a separate connection, so the ingestion
finishes queue up behind it, and then release it. Each test waits until the expected sessions are blocked on the lock
(pg_stat_activity), so the interleaving is set up, not guessed.

(a) two replacements finish together; (b) a newer one finishes first; (c) a failed replacement leaves the current
version answering; (d) the switch is atomic for retrieval; deadlock ordering: a delete and a finish on the same
document, which must not deadlock.
"""

import asyncio
import threading
import time
import uuid
from typing import Any

import psycopg
import pytest

from grounded_answers.embedding.pinned import MODEL_NAME, MODEL_REVISION
from grounded_answers.ingest.pipeline import IngestJob, Outcome, run_ingestion
from grounded_answers.models import EMBEDDING_DIMENSIONS
from grounded_answers.storage.postgres_store import PostgresObjectStore

from .auth_support import Accounts
from .database import DbHandle
from .ingest_support import ExplodingEmbedder, FakeEmbedder, Made, make_version, worker_sessions
from .seed import Seeded

pytestmark = [pytest.mark.db, pytest.mark.anyio]

MARKDOWN = "text/markdown"
POLICY_MD = b"# 3 Probation\n\n3.2 Probation is six months for LTV drivers.\n"


def _admin_url(handle: DbHandle) -> str:
    return handle.admin_test_url()


def _wait_for_lock_waiters(admin: psycopg.Connection, at_least: int, timeout: float = 30.0) -> None:
    """Block until at least `at_least` sessions of this database are waiting on a lock."""
    deadline = time.monotonic() + timeout
    while True:
        waiting = admin.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND datname = current_database()"
        ).fetchone()
        assert waiting is not None
        if int(waiting[0]) >= at_least:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"fewer than {at_least} sessions reached the lock")
        time.sleep(0.05)


def _hold_document(handle: DbHandle, document: uuid.UUID) -> psycopg.Connection:
    """A transaction that holds the document's row lock until the caller commits it."""
    holder = psycopg.connect(_admin_url(handle))
    holder.execute("SELECT id FROM grounded.documents WHERE id = %s FOR UPDATE", (document,))
    return holder


def _ready_current(
    admin: psycopg.Connection, made: Made, seeded: Seeded, *, chunks: int = 0
) -> tuple[uuid.UUID, uuid.UUID]:
    """A live document whose version 1 is ready and current, with `chunks` chunks."""
    document, version = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="ready",
    )
    admin.execute(
        "UPDATE grounded.documents SET current_version_id = %s WHERE id = %s", (version, document)
    )
    for position in range(chunks):
        admin.execute(
            "INSERT INTO grounded.chunks (tenant_id, version_id, position, body, embedding, "
            "embedding_model, embedding_model_version) VALUES (%s, %s, %s, 'old text', %s::extensions.vector, %s, %s)",
            (
                seeded.a.tenant,
                version,
                position,
                "[" + ",".join("0.1" for _ in range(EMBEDDING_DIMENSIONS)) + "]",
                MODEL_NAME,
                MODEL_REVISION,
            ),
        )
    return document, version


def _queued_replacement(
    admin: psycopg.Connection, made: Made, seeded: Seeded, document: uuid.UUID, number: int
) -> uuid.UUID:
    """A queued version with bytes, moved onto the document. Its job is run by the test, not by the queue."""
    _, version = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=POLICY_MD,
        status="queued",
        version_no=number,
    )
    admin.execute(
        "UPDATE grounded.document_versions SET document_id = %s WHERE id = %s", (document, version)
    )
    return version


def _job(seeded: Seeded, document: uuid.UUID, version: uuid.UUID) -> IngestJob:
    return IngestJob(
        tenant_id=seeded.a.tenant,
        document_id=document,
        version_id=version,
        attempt_token=uuid.uuid4(),
        lease_seconds=120,
        retention_days=30,
    )


async def _ingest(factory: Any, embedder: Any, job: IngestJob) -> Outcome:
    return await run_ingestion(
        session_factory=factory, object_store=PostgresObjectStore(), embedder=embedder, job=job
    )


def _swap_outcomes(admin: psycopg.Connection, document: uuid.UUID) -> list[str]:
    rows = admin.execute(
        "SELECT detail->>'outcome' FROM grounded.audit_log WHERE action = 'document.swap' AND target_id = %s "
        "ORDER BY created_at, id",
        (document,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _current(admin: psycopg.Connection, document: uuid.UUID) -> uuid.UUID | None:
    row = admin.execute(
        "SELECT current_version_id FROM grounded.documents WHERE id = %s", (document,)
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0])) if row[0] is not None else None


def _visible_chunk_versions(handle: DbHandle, tenant: uuid.UUID) -> frozenset[str]:
    """The versions whose chunks retrieval shows to app_user right now (its own statement, its own snapshot)."""
    conn = psycopg.connect(
        handle.url_for("app_user", handle.passwords["app_user"]), autocommit=True
    )
    try:
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant),))
        rows = conn.execute("SELECT DISTINCT version_id FROM grounded.chunks").fetchall()
        return frozenset(str(row[0]) for row in rows)
    finally:
        conn.close()


# ---- (a) two replacements finish together ----


async def test_two_replacements_that_finish_together_leave_one_current_and_retire_the_old_once(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document, old = _ready_current(admin, made, seeded)
    v2 = _queued_replacement(admin, made, seeded, document, 2)
    v3 = _queued_replacement(admin, made, seeded, document, 3)
    holder = _hold_document(grounded_db, document)
    async with worker_sessions(grounded_db) as factory:
        tasks = [
            asyncio.create_task(_ingest(factory, FakeEmbedder(), _job(seeded, document, v2))),
            asyncio.create_task(_ingest(factory, FakeEmbedder(), _job(seeded, document, v3))),
        ]
        await asyncio.to_thread(_wait_for_lock_waiters, admin, 2)
        holder.commit()
        holder.close()
        outcomes = await asyncio.gather(*tasks)

    assert outcomes == [Outcome.READY, Outcome.READY]
    assert _current(admin, document) == v3, (
        "the higher version number ends current, in either order"
    )
    assert sorted(_swap_outcomes(admin, document)) in (
        ["current", "current"],
        ["current", "superseded"],
    )
    retired = admin.execute(
        "SELECT COALESCE(SUM((detail->>'retired')::int), 0) FROM grounded.audit_log "
        "WHERE action = 'document.swap' AND target_id = %s",
        (document,),
    ).fetchone()
    assert retired is not None and retired[0] == 2, "v1 and v2 are each retired exactly once"
    for version in (old, v2):
        row = admin.execute(
            "SELECT status FROM grounded.document_versions WHERE id = %s", (version,)
        ).fetchone()
        assert row == ("retired",)


# ---- (b) a newer replacement finishes first ----


async def test_a_newer_replacement_that_finishes_first_stays_current(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document, old = _ready_current(admin, made, seeded)
    v2 = _queued_replacement(admin, made, seeded, document, 2)
    v3 = _queued_replacement(admin, made, seeded, document, 3)
    async with worker_sessions(grounded_db) as factory:
        assert await _ingest(factory, FakeEmbedder(), _job(seeded, document, v3)) is Outcome.READY
        assert await _ingest(factory, FakeEmbedder(), _job(seeded, document, v2)) is Outcome.READY

    assert _current(admin, document) == v3
    assert _swap_outcomes(admin, document) == ["current", "superseded"]
    row = admin.execute(
        "SELECT status FROM grounded.document_versions WHERE id = %s", (v2,)
    ).fetchone()
    assert row == ("retired",), "v2 is retired at once, as superseded"
    assert admin.execute(
        "SELECT status FROM grounded.document_versions WHERE id = %s", (old,)
    ).fetchone() == ("retired",)


# ---- (c) a failed replacement leaves the current version answering ----


async def test_a_failed_replacement_leaves_the_current_version_answering(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document, current = _ready_current(admin, made, seeded, chunks=2)
    v2 = _queued_replacement(admin, made, seeded, document, 2)
    async with worker_sessions(grounded_db) as factory:
        outcome = await _ingest(
            factory, ExplodingEmbedder("secret-text"), _job(seeded, document, v2)
        )

    assert outcome is Outcome.FAILED
    assert _current(admin, document) == current
    assert admin.execute(
        "SELECT status FROM grounded.document_versions WHERE id = %s", (current,)
    ).fetchone() == ("ready",)
    assert _visible_chunk_versions(grounded_db, seeded.a.tenant) == frozenset({str(current)})


# ---- (d) the switch is atomic for retrieval ----


async def test_retrieval_switches_atomically_from_the_old_version_to_the_new_one(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    """While the finish waits on the document lock, retrieval shows only the old version. After the commit it shows
    only the new one. Every sample taken during the switch is one of those two states: never neither, never both."""
    document, old = _ready_current(admin, made, seeded, chunks=2)
    v2 = _queued_replacement(admin, made, seeded, document, 2)
    samples: list[frozenset[str]] = []
    stop = threading.Event()

    def observe() -> None:
        while not stop.is_set():
            samples.append(_visible_chunk_versions(grounded_db, seeded.a.tenant))

    observer = threading.Thread(target=observe)
    observer.start()
    holder = _hold_document(grounded_db, document)
    try:
        async with worker_sessions(grounded_db) as factory:
            task = asyncio.create_task(_ingest(factory, FakeEmbedder(), _job(seeded, document, v2)))
            await asyncio.to_thread(_wait_for_lock_waiters, admin, 1)
            await asyncio.sleep(0.5)
            holder.commit()
            holder.close()
            assert await task is Outcome.READY
    finally:
        stop.set()
        observer.join(timeout=30)

    assert samples, "the observer must have sampled"
    assert set(samples) <= {frozenset({str(old)}), frozenset({str(v2)})}, (
        "retrieval must never show neither or both"
    )
    assert frozenset({str(old)}) in samples, "the old version answered while the finish was blocked"
    assert samples[-1] == frozenset({str(v2)}), "the new version answers once the finish commits"


# ---- deadlock ordering: a delete and a finish on the same document ----


async def test_a_delete_and_a_finish_on_the_same_document_do_not_deadlock(
    grounded_db: DbHandle, accounts: Accounts, admin: psycopg.Connection, made: Made
) -> None:
    """The finish takes the document lock before any version row. A delete takes the document first, then retires the
    versions. The two therefore cannot wait on each other. Here the delete holds the document while the finish waits
    for it, and the delete then retires the version the finish is working on. Both must finish, with no deadlock."""
    seeded = accounts.seeded
    document, _current = _ready_current(admin, made, seeded)
    v2 = _queued_replacement(admin, made, seeded, document, 2)
    async with worker_sessions(grounded_db) as factory:
        finish = asyncio.create_task(_ingest(factory, FakeEmbedder(), _job(seeded, document, v2)))
        deadline = time.monotonic() + 30
        while admin.execute(
            "SELECT status FROM grounded.document_versions WHERE id = %s", (v2,)
        ).fetchone() != ("processing",):
            assert time.monotonic() < deadline, "the finish never claimed the version"
            await asyncio.sleep(0.05)
        # The delete: lock the document (its first step), then retire the version, in one transaction.
        delete = psycopg.connect(grounded_db.url_for("app_user", grounded_db.passwords["app_user"]))
        try:
            delete.execute(
                "SELECT set_config('app.current_tenant', %s, true)", (str(seeded.a.tenant),)
            )
            locked = delete.execute(
                "SELECT id FROM grounded.documents WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (document,),
            ).fetchone()
            assert locked is not None
            await asyncio.to_thread(_wait_for_lock_waiters, admin, 1)
            delete.execute(
                "UPDATE grounded.document_versions SET status = 'retired', retired_at = now(), "
                "purge_after = now() + interval '30 days' WHERE id = %s AND status = 'processing'",
                (v2,),
            )
            delete.execute(
                "UPDATE grounded.documents SET deleted_at = now(), purge_after = now() + interval '30 days' "
                "WHERE id = %s",
                (document,),
            )
            delete.commit()
        finally:
            delete.close()
        outcome = await finish

    assert outcome is Outcome.LOST, "the version was retired under the finish: it loses cleanly"
    assert admin.execute(
        "SELECT status FROM grounded.document_versions WHERE id = %s", (v2,)
    ).fetchone() == ("retired",)
    assert admin.execute(
        "SELECT count(*) FROM grounded.chunks WHERE version_id = %s", (v2,)
    ).fetchone() == (0,)

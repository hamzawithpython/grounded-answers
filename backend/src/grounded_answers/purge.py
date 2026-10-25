"""
The purge (ADR 0027, ADR 0007, SECURITY.md "Data retention").

The worker's timer runs a sweep every fifteen minutes (jobs/sweeps.py). It asks the definer grounded.enqueue_purge_jobs
(migration 0014) for the due items, and defers one job for each. A job removes one item in one transaction. It locks the document row first, the same order as replacement, the
swap and delete (ADR 0024):

- kind 'document': a deleted document whose purge_after has passed. Its versions go first (children first), then the
  pointer is cleared, then the document. The versions of a deleted document were retired with the document, at the
  same moment, so they share its purge_after and none is purged early.
- kind 'version': a retired version of a live document whose purge_after has passed. It is never the current version.

A job checks due-ness itself, under the lock. So a job that runs after its item is gone, or before it is due, changes
nothing, and a re-run after a success is a no-op. A failure rolls the whole item back: the rows stay retired, and the
next sweep defers the item again.

Every write is counted (expect_rowcount, affected_rows). The audit row holds counts only: no title, no filename, no
content. It names the item by its id, and the id is in the audit row's target, which is how every audit row names it.
"""

import json
import logging
import uuid
from typing import Final, Literal

import procrastinate
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from grounded_answers.jobs.tasks import PURGE_ITEM
from grounded_answers.rowcount import affected_rows, expect_rowcount
from grounded_answers.tenant_db import WorkerContext, begin_worker_transaction

logger = logging.getLogger(__name__)


# Locks the document. It also reports whether the document is deleted and its retention has passed, so the due check
# happens under the lock. The worker's lock policy admits deleted documents (observed, test_worker_document_lock).
_LOCK_DOCUMENT: Final = text(
    "SELECT current_version_id, deleted_at IS NOT NULL AS deleted, deleted_at IS NOT NULL AND purge_after <= now() AS due "
    "FROM grounded.documents WHERE id = :document_id AND tenant_id = :tenant_id FOR UPDATE"
)
_VERSION_DOCUMENT: Final = text(
    "SELECT document_id FROM grounded.document_versions WHERE id = :version_id AND tenant_id = :tenant_id"
)
_VERSION_STATE: Final = text(
    "SELECT status, purge_after <= now() FROM grounded.document_versions "
    "WHERE id = :version_id AND tenant_id = :tenant_id"
)
_MARK_DOCUMENT_VERSIONS: Final = text(
    "UPDATE grounded.document_versions SET status = 'purging' WHERE document_id = :document_id "
    "AND tenant_id = :tenant_id AND status = 'retired' AND purge_after <= now()"
)
_MARK_VERSION: Final = text(
    "UPDATE grounded.document_versions SET status = 'purging' WHERE id = :version_id "
    "AND tenant_id = :tenant_id AND status = 'retired' AND purge_after <= now()"
)
_DELETE_CHUNKS_OF_PURGING: Final = text(
    "DELETE FROM grounded.chunks WHERE version_id IN (SELECT id FROM grounded.document_versions "
    "WHERE document_id = :document_id AND tenant_id = :tenant_id AND status = 'purging')"
)
_DELETE_BLOBS_OF_PURGING: Final = text(
    "DELETE FROM grounded.document_blobs WHERE version_id IN (SELECT id FROM grounded.document_versions "
    "WHERE document_id = :document_id AND tenant_id = :tenant_id AND status = 'purging')"
)
_DELETE_CHUNKS_OF_VERSION: Final = text(
    "DELETE FROM grounded.chunks WHERE version_id = :version_id AND tenant_id = :tenant_id"
)
_DELETE_BLOB_OF_VERSION: Final = text(
    "DELETE FROM grounded.document_blobs WHERE version_id = :version_id AND tenant_id = :tenant_id"
)
_CLEAR_POINTER: Final = text(
    "UPDATE grounded.documents SET current_version_id = NULL WHERE id = :document_id "
    "AND tenant_id = :tenant_id AND deleted_at IS NOT NULL"
)
_DELETE_PURGING_VERSIONS: Final = text(
    "DELETE FROM grounded.document_versions WHERE document_id = :document_id AND tenant_id = :tenant_id "
    "AND status = 'purging'"
)
_DELETE_PURGED_VERSION: Final = text(
    "DELETE FROM grounded.document_versions WHERE id = :version_id AND tenant_id = :tenant_id AND status = 'purging'"
)
_COUNT_VERSIONS: Final = text(
    "SELECT count(*) FROM grounded.document_versions WHERE document_id = :document_id AND tenant_id = :tenant_id"
)
_DELETE_DOCUMENT: Final = text(
    "DELETE FROM grounded.documents WHERE id = :document_id AND tenant_id = :tenant_id AND deleted_at IS NOT NULL"
)
_INSERT_PURGE_AUDIT: Final = text(
    "INSERT INTO grounded.audit_log (id, tenant_id, actor_user_id, action, target_type, target_id, detail) "
    "VALUES (:id, :tenant_id, NULL, :action, :target_type, :target_id, CAST(:detail AS jsonb))"
)
_ENQUEUE: Final = text(
    "SELECT out_enqueued, out_already_waiting FROM grounded.enqueue_purge_jobs()"
)


async def _audit(
    session: AsyncSession,
    tenant: uuid.UUID,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    counts: dict[str, int],
) -> None:
    await session.execute(
        _INSERT_PURGE_AUDIT,
        {
            "id": str(uuid.uuid4()),
            "tenant_id": str(tenant),
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id),
            "detail": json.dumps(counts),
        },
    )


async def _purge_document(session: AsyncSession, tenant: uuid.UUID, document: uuid.UUID) -> str:
    locked = (
        await session.execute(
            _LOCK_DOCUMENT, {"document_id": str(document), "tenant_id": str(tenant)}
        )
    ).first()
    if locked is None:
        return "gone"
    if not locked[2]:
        return "not_due"

    params = {"document_id": str(document), "tenant_id": str(tenant)}
    marked = affected_rows(await session.execute(_MARK_DOCUMENT_VERSIONS, params))
    chunks = affected_rows(await session.execute(_DELETE_CHUNKS_OF_PURGING, params))
    blobs = affected_rows(await session.execute(_DELETE_BLOBS_OF_PURGING, params))
    # The pointer goes before the versions: the foreign key is RESTRICT (ARCHITECTURE 5.5, steps 4 and 5).
    expect_rowcount(await session.execute(_CLEAR_POINTER, params), 1, "purge.clear_pointer")
    removed = affected_rows(await session.execute(_DELETE_PURGING_VERSIONS, params))
    if removed != marked:
        raise RuntimeError("the purging versions removed do not match the versions marked")
    remaining = (await session.execute(_COUNT_VERSIONS, params)).scalar_one()
    if int(remaining) != 0:
        # A version that is not yet due, or in another state, keeps the document. The next sweep will handle it.
        raise RuntimeError("a deleted document still has versions it was not allowed to purge")
    expect_rowcount(await session.execute(_DELETE_DOCUMENT, params), 1, "purge.delete_document")
    await _audit(
        session,
        tenant,
        "document.purged",
        "document",
        document,
        {"versions": marked, "chunks": chunks, "blobs": blobs},
    )
    return "purged"


async def _purge_version(session: AsyncSession, tenant: uuid.UUID, version: uuid.UUID) -> str:
    owner = (
        await session.execute(
            _VERSION_DOCUMENT, {"version_id": str(version), "tenant_id": str(tenant)}
        )
    ).first()
    if owner is None:
        return "gone"
    # The document row is locked before the version is read or written: the same order as every other path (ADR 0024).
    document: uuid.UUID = owner[0]
    locked = (
        await session.execute(
            _LOCK_DOCUMENT, {"document_id": str(document), "tenant_id": str(tenant)}
        )
    ).first()
    if locked is None:
        return "gone"
    if locked[1]:
        return "covered_by_document"
    state = (
        await session.execute(
            _VERSION_STATE, {"version_id": str(version), "tenant_id": str(tenant)}
        )
    ).first()
    if state is None:
        return "gone"
    if state[0] != "retired" or not state[1]:
        return "not_due"
    if locked[0] == version:
        return "current"

    params = {"version_id": str(version), "tenant_id": str(tenant)}
    expect_rowcount(await session.execute(_MARK_VERSION, params), 1, "purge.mark_version")
    chunks = affected_rows(await session.execute(_DELETE_CHUNKS_OF_VERSION, params))
    blobs = affected_rows(await session.execute(_DELETE_BLOB_OF_VERSION, params))
    expect_rowcount(
        await session.execute(_DELETE_PURGED_VERSION, params), 1, "purge.delete_version"
    )
    await _audit(
        session,
        tenant,
        "document_version.purged",
        "document_version",
        version,
        {"chunks": chunks, "blobs": blobs},
    )
    return "purged"


async def purge_item(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    kind: Literal["document", "version"],
    item_id: uuid.UUID,
) -> str:
    """Purge one due item in one transaction. Returns the outcome, which is logged without ids."""
    async with begin_worker_transaction(session_factory, WorkerContext(tenant_id)) as session:
        if kind == "document":
            outcome = await _purge_document(session, tenant_id, item_id)
        else:
            outcome = await _purge_version(session, tenant_id, item_id)
    logger.info("purge.item kind=%s outcome=%s", kind, outcome)
    return outcome


async def sweep_purge(*, session_factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    """Defer one purge job per due item (grounded.enqueue_purge_jobs). Returns (enqueued, already_waiting)."""
    async with session_factory() as session:
        row = (await session.execute(_ENQUEUE)).one()
        await session.commit()
    enqueued, already_waiting = int(row[0]), int(row[1])
    logger.info("purge.sweep enqueued=%d already_waiting=%d", enqueued, already_waiting)
    return enqueued, already_waiting


def register_purge_task(
    app: procrastinate.App, *, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Register the purge job on the worker's app. The sweep that enqueues these jobs runs in the worker's timer."""

    @app.task(name=PURGE_ITEM)
    async def purge_item_task(tenant_id: str, kind: str, item_id: str) -> None:
        if kind not in ("document", "version"):
            raise ValueError("the purge kind must be 'document' or 'version'")
        await purge_item(
            session_factory=session_factory,
            tenant_id=uuid.UUID(tenant_id),
            kind="document" if kind == "document" else "version",
            item_id=uuid.UUID(item_id),
        )

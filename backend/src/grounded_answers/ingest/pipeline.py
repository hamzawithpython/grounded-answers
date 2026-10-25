"""
Ingest one document version (ADR 0022).

Steps, each in its own transaction, except the last two which share one:
1. Claim (short transaction): queued -> processing, or take over a processing version whose lease expired.
   A fresh lease held by another attempt raises LeaseHeld (retried). A version in any other state is skipped.
2. Read the bytes, extract (sandbox child process) and chunk (S1). Embed (model loaded once per process).
   None of this runs inside a database transaction, so no row lock is held while it runs.
3. Write (one transaction): check the fencing token, replace the version's chunks, mark the version ready,
   set the document's current version if it has none, and write one audit row. Any failure rolls all of it back.

A failure in step 2 that the document causes (unreadable, no text, timeout, memory, embedding) is written as
status failed with its generic reason (step 4). Database errors are not caught here: they propagate, and the job
is retried (jobs/ingest_task.py).

Each attempt carries a token. The worker derives it from the job id, so a retry of the same job keeps its token
and may resume its own lease. Another job for the same version has a different token, and waits (LeaseHeld).
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from grounded_answers.embedding.model import Embedder
from grounded_answers.embedding.pinned import MODEL_NAME, MODEL_REVISION
from grounded_answers.extract.sandbox import (
    REASON_NO_TEXT,
    REASON_UNREADABLE,
    ExtractionFailed,
    extract,
)
from grounded_answers.ingest.chunking import Chunk, ChunkTooLong, embedding_texts, section_chunks
from grounded_answers.ingest.failures import (
    FAILURE_REASONS,
    REASON_EMBEDDING,
    LeaseHeld,
    LostLease,
    VersionNotFound,
)
from grounded_answers.models import AuditLog
from grounded_answers.models import Chunk as ChunkRow
from grounded_answers.rowcount import (
    RowcountMismatchError,
    affected_rows,
)
from grounded_answers.storage.object_store import ObjectNotFound, ObjectStore
from grounded_answers.tenant_db import WorkerContext, begin_worker_transaction
from grounded_answers.upload.detect import CONTENT_TYPE

logger = logging.getLogger(__name__)

# Rows per INSERT statement. One statement per batch, so the row count can be checked (rowcount.py).
INSERT_BATCH: Final = 100
KIND_BY_CONTENT_TYPE: Final[dict[str, str]] = {
    content_type: kind.value for kind, content_type in CONTENT_TYPE.items()
}


class Outcome(StrEnum):
    READY = "ready"
    FAILED = "failed"
    SKIPPED = "skipped"
    LOST = "lost"


@dataclass(frozen=True)
class IngestJob:
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    attempt_token: uuid.UUID
    lease_seconds: int


@dataclass(frozen=True)
class _Claimed:
    token: uuid.UUID
    title: str
    version_no: int
    content_type: str


_VERSION_STATE: Final = text(
    "SELECT v.status, v.content_type, v.version_no, d.title, "
    "v.processing_token = :token AS mine, "
    "(v.processing_started_at > now() - make_interval(secs => :lease)) AS lease_fresh "
    "FROM grounded.document_versions AS v "
    "JOIN grounded.documents AS d ON d.id = v.document_id AND d.tenant_id = v.tenant_id "
    "WHERE v.id = :version_id AND v.document_id = :document_id"
)
_CLAIM: Final = text(
    "UPDATE grounded.document_versions SET status = 'processing', "
    "processing_started_at = now(), processing_token = :token "
    "WHERE id = :version_id AND status = 'queued'"
)
_TAKE_OVER: Final = text(
    "UPDATE grounded.document_versions SET processing_started_at = now(), processing_token = :token "
    "WHERE id = :version_id AND status = 'processing' "
    "AND processing_started_at <= now() - make_interval(secs => :lease)"
)
_RENEW: Final = text(
    "UPDATE grounded.document_versions SET processing_started_at = now() "
    "WHERE id = :version_id AND status = 'processing' AND processing_token = :token"
)
# The fencing check: takes the row lock for this transaction and proves this attempt still holds the version.
_FENCE: Final = text(
    "UPDATE grounded.document_versions SET processing_started_at = processing_started_at "
    "WHERE id = :version_id AND status = 'processing' AND processing_token = :token"
)
_DELETE_CHUNKS: Final = text("DELETE FROM grounded.chunks WHERE version_id = :version_id")
_FINISH: Final = text(
    "UPDATE grounded.document_versions SET status = 'ready', "
    "processing_started_at = NULL, processing_token = NULL "
    "WHERE id = :version_id AND status = 'processing' AND processing_token = :token"
)
_FAIL: Final = text(
    "UPDATE grounded.document_versions SET status = 'failed', failure_reason = :reason, "
    "processing_started_at = NULL, processing_token = NULL "
    "WHERE id = :version_id AND status = 'processing' AND processing_token = :token"
)
# The document row is the serialization point for its versions (ADR 0024). The finish takes it first, in the same
# order as replacement and delete: document, then versions.
_LOCK_DOCUMENT: Final = text(
    "SELECT id FROM grounded.documents WHERE id = :document_id AND tenant_id = :tenant_id "
    "AND deleted_at IS NULL FOR UPDATE"
)
# The swap (migration 0013): makes this version current if it is newer than the current one, or retires it as
# superseded. Returns 'current' or 'superseded'.
_SWAP: Final = text("SELECT grounded.swap_current_version(:document_id, :version_id)")


async def _claim(
    session_factory: async_sessionmaker[AsyncSession], job: IngestJob
) -> _Claimed | Outcome:
    async with begin_worker_transaction(session_factory, WorkerContext(job.tenant_id)) as session:
        row = (
            await session.execute(
                _VERSION_STATE,
                {
                    "token": job.attempt_token,
                    "lease": job.lease_seconds,
                    "version_id": job.version_id,
                    "document_id": job.document_id,
                },
            )
        ).first()
        if row is None:
            raise VersionNotFound("version not found for this tenant")
        status, content_type, version_no, title, mine, lease_fresh = row
        ids = {"token": job.attempt_token, "version_id": job.version_id}
        # A race with another job loses its UPDATE (0 rows). That is a transient wait, not an error:
        # the winner finishes, and the retry then sees 'ready' and skips.
        if status == "queued":
            if affected_rows(await session.execute(_CLAIM, ids)) != 1:
                raise LeaseHeld("the version was claimed by another attempt")
        elif status == "processing" and mine:
            # A retry of this job: resume its own lease and renew it.
            if affected_rows(await session.execute(_RENEW, ids)) != 1:
                raise LostLease("the lease was taken over before renewal")
        elif status == "processing" and not lease_fresh:
            params = {**ids, "lease": job.lease_seconds}
            if affected_rows(await session.execute(_TAKE_OVER, params)) != 1:
                raise LeaseHeld("the lease was taken over by another attempt")
        elif status == "processing":
            raise LeaseHeld("another attempt holds a fresh lease")
        else:
            logger.info("ingest.skipped state=%s", status)
            return Outcome.SKIPPED
        return _Claimed(
            token=job.attempt_token,
            title=str(title),
            version_no=int(version_no),
            content_type=str(content_type),
        )


async def _read_bytes(
    session_factory: async_sessionmaker[AsyncSession], object_store: ObjectStore, job: IngestJob
) -> bytes:
    async with begin_worker_transaction(session_factory, WorkerContext(job.tenant_id)) as session:
        return await object_store.get(session, tenant_id=job.tenant_id, version_id=job.version_id)


async def mark_failed(
    session_factory: async_sessionmaker[AsyncSession],
    job: IngestJob,
    reason: str,
) -> Outcome:
    """Set the version to failed with its generic reason, if this attempt still holds it (fenced)."""
    if reason not in FAILURE_REASONS:
        raise ValueError("not a generic ingestion failure reason")
    async with begin_worker_transaction(session_factory, WorkerContext(job.tenant_id)) as session:
        result = await session.execute(
            _FAIL,
            {"reason": reason, "version_id": job.version_id, "token": job.attempt_token},
        )
        if affected_rows(result) != 1:
            logger.info("ingest.lost_lease")
            return Outcome.LOST
        await _audit(session, job, "document.failed", {"reason": reason})
    logger.info("ingest.failed reason=%s", reason)
    return Outcome.FAILED


async def _audit(
    session: AsyncSession, job: IngestJob, action: str, detail: dict[str, str]
) -> None:
    """One audit row, with the action and the reason only: no document text and no file name (ADR 0014)."""
    await session.execute(
        insert(AuditLog).values(
            id=uuid.uuid4(),
            tenant_id=job.tenant_id,
            actor_user_id=None,
            action=action,
            target_type="document_version",
            target_id=job.version_id,
            detail=detail,
        )
    )


async def _write_ready(
    session_factory: async_sessionmaker[AsyncSession],
    job: IngestJob,
    chunks: list[Chunk],
    vectors: list[list[float]],
) -> Outcome:
    async with begin_worker_transaction(session_factory, WorkerContext(job.tenant_id)) as session:
        locked = (
            await session.execute(
                _LOCK_DOCUMENT, {"document_id": job.document_id, "tenant_id": job.tenant_id}
            )
        ).first()
        if locked is None:
            raise LostLease("the document is not live")
        fence = await session.execute(
            _FENCE, {"version_id": job.version_id, "token": job.attempt_token}
        )
        if affected_rows(fence) != 1:
            raise LostLease("the version was taken over")
        await session.execute(_DELETE_CHUNKS, {"version_id": job.version_id})
        rows: list[dict[str, Any]] = [
            {
                "tenant_id": job.tenant_id,
                "version_id": job.version_id,
                "position": chunk.position,
                "section_number": chunk.section_number,
                "heading": chunk.heading,
                "clause_labels": list(chunk.clause_labels),
                "body": chunk.body,
                "embedding": vector,
                "embedding_model": MODEL_NAME,
                "embedding_model_version": MODEL_REVISION,
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        for start in range(0, len(rows), INSERT_BATCH):
            batch = rows[start : start + INSERT_BATCH]
            # A multi-row INSERT does not report a reliable rowcount through SQLAlchemy (it reports -1), so the
            # check counts the rows the statement returns. That is the same guarantee: every row must come back.
            inserted = (
                await session.execute(insert(ChunkRow).values(batch).returning(ChunkRow.position))
            ).all()
            if len(inserted) != len(batch):
                raise RowcountMismatchError(
                    f"ingest.chunks.insert returned {len(inserted)} rows, expected {len(batch)}"
                )
        finish = await session.execute(
            _FINISH, {"version_id": job.version_id, "token": job.attempt_token}
        )
        if affected_rows(finish) != 1:
            raise LostLease("the version was taken over before finish")
        # The swap decides whether this version becomes current (ADR 0024). It runs in this transaction, so the
        # pointer and the chunks become visible together, or not at all.
        outcome = (
            await session.execute(
                _SWAP, {"version_id": job.version_id, "document_id": job.document_id}
            )
        ).scalar_one()
        await _audit(session, job, "document.ready", {})
    logger.info("ingest.ready chunks=%d swap=%s", len(chunks), outcome)
    return Outcome.READY


async def run_ingestion(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    object_store: ObjectStore,
    embedder: Embedder,
    job: IngestJob,
) -> Outcome:
    """Run one attempt. Returns the outcome. Raises VersionNotFound, LeaseHeld and database errors."""
    logger.info("ingest.started")
    claimed = await _claim(session_factory, job)
    if isinstance(claimed, Outcome):
        return claimed
    try:
        data = await _read_bytes(session_factory, object_store, job)
        kind = KIND_BY_CONTENT_TYPE.get(claimed.content_type)
        if kind is None:
            return await mark_failed(session_factory, job, REASON_UNREADABLE)
        extracted = await asyncio.to_thread(extract, kind, data)
        chunks = section_chunks(extracted)
        if not chunks:
            return await mark_failed(session_factory, job, REASON_NO_TEXT)
        texts = embedding_texts(chunks, claimed.title, claimed.version_no)
        vectors = await _embed(embedder, texts)
        if vectors is None:
            return await mark_failed(session_factory, job, REASON_EMBEDDING)
        return await _write_ready(session_factory, job, chunks, vectors)
    except ObjectNotFound:
        return await mark_failed(session_factory, job, REASON_UNREADABLE)
    except ExtractionFailed as exc:
        return await mark_failed(session_factory, job, exc.reason)
    except ChunkTooLong:
        return await mark_failed(session_factory, job, REASON_UNREADABLE)
    except LostLease:
        logger.info("ingest.lost_lease")
        return Outcome.LOST


async def _embed(embedder: Embedder, texts: list[str]) -> list[list[float]] | None:
    """The model's failures are one reason, 'embedding'. The exception class is logged, never its message."""
    try:
        return await asyncio.to_thread(embedder.embed_passages, texts)
    except (
        Exception
    ) as exc:  # the model runtime raises several types; each one is the same failure to us
        logger.error("ingest.embedding_failed exception=%s", type(exc).__name__)
        return None

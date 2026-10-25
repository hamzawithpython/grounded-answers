"""
The ingestion task (ADR 0015, ADR 0022). One job ingests one document version.

Retry rules:
- Transient errors (database connection or timeout, lease held by another attempt) are retried with exponential
  waits, up to RETRY_MAX_ATTEMPTS. Only the exception types listed in RETRYABLE are retried (Procrastinate retries
  every exception when no list is given).
- Everything else ends the job at once: a missing version (poison), a corrupt upload, a failed extraction. Those
  are written to the version as a generic reason by pipeline.py and the job is not retried.
- On the last attempt, a transient error marks the version failed with LAST_ATTEMPT_REASON, so it does not stay in
  processing with no job to retry it. A hard-killed worker never reaches this code (ADR 0022, D5); the stalled-job
  retry is the next item in PROGRESS.md.

Procrastinate increments job.attempts when it fetches the job, so attempt k sees attempts == k. The last attempt
is the one where attempts >= RETRY_MAX_ATTEMPTS, the same test the retry decision uses.
"""

import logging
import uuid
from typing import Final

import procrastinate
import psycopg
from sqlalchemy.exc import OperationalError as SqlAlchemyOperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from grounded_answers.embedding.model import Embedder
from grounded_answers.ingest.failures import REASON_RETRIES_EXHAUSTED, LeaseHeld
from grounded_answers.ingest.pipeline import IngestJob, mark_failed, run_ingestion
from grounded_answers.jobs.tasks import INGEST_DOCUMENT
from grounded_answers.storage.object_store import ObjectStore

logger = logging.getLogger(__name__)

RETRY_MAX_ATTEMPTS: Final = 5
RETRYABLE: Final = (
    psycopg.OperationalError,
    SqlAlchemyOperationalError,
    TimeoutError,
    ConnectionError,
    LeaseHeld,
)
# The reason written when a transient error exhausts its attempts (ADR 0022, decision D5).
LAST_ATTEMPT_REASON: Final = REASON_RETRIES_EXHAUSTED
_JOB_NAMESPACE: Final = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f")


def attempt_token(job_id: int) -> uuid.UUID:
    """The token a job's attempts carry. Stable across retries of one job, different for any other job."""
    return uuid.uuid5(_JOB_NAMESPACE, f"ingest:{job_id}")


def register_ingest_task(
    app: procrastinate.App,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    object_store: ObjectStore,
    embedder: Embedder,
    lease_seconds: int,
    retention_days: int,
) -> None:
    retry = procrastinate.RetryStrategy(
        max_attempts=RETRY_MAX_ATTEMPTS,
        exponential_wait=5,
        retry_exceptions=RETRYABLE,
    )

    @app.task(name=INGEST_DOCUMENT, retry=retry, pass_context=True)
    async def ingest_document(
        context: procrastinate.JobContext,
        tenant_id: str,
        document_id: str,
        version_id: str,
    ) -> None:
        job_id = context.job.id
        if job_id is None:
            raise RuntimeError("the job has no id; Procrastinate always sets one")
        job = IngestJob(
            tenant_id=uuid.UUID(tenant_id),
            document_id=uuid.UUID(document_id),
            version_id=uuid.UUID(version_id),
            attempt_token=attempt_token(job_id),
            lease_seconds=lease_seconds,
            retention_days=retention_days,
        )
        is_last = context.job.attempts >= RETRY_MAX_ATTEMPTS
        try:
            await run_ingestion(
                session_factory=session_factory,
                object_store=object_store,
                embedder=embedder,
                job=job,
            )
        except RETRYABLE as exc:
            logger.warning("ingest.retry exception=%s", type(exc).__name__)
            if is_last:
                await _give_up(session_factory, job)
            raise

    _ = ingest_document  # registered on the app by the decorator


async def _give_up(session_factory: async_sessionmaker[AsyncSession], job: IngestJob) -> None:
    """Mark the version failed on the last attempt. If that write fails too, log it and let the original error stand."""
    try:
        await mark_failed(session_factory, job, LAST_ATTEMPT_REASON)
    except RETRYABLE as exc:
        logger.error("ingest.give_up_failed exception=%s", type(exc).__name__)

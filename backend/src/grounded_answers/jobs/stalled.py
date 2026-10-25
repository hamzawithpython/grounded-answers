"""
The stalled-version retry (ADR 0023, as amended). The worker's timer runs this sweep (jobs/sweeps.py); it closes the gap in
ADR 0022, D5.

A worker killed hard leaves a version in processing, with its lease expired and no job to retry it. The sweep calls
the definer function grounded.enqueue_stalled_ingest (migration 0011). The function finds those versions, and defers
one ingest job for each one. The ingest job takes over the expired lease (ADR 0022, D1), so the fencing token still
decides who writes.

The worker never defers a job itself. The function is the only way worker_user enqueues, and it fixes the task name.

Repeated sweeps do not pile up jobs. Each job carries the queueing lock "ingest:<version_id>". While one job for a
version is waiting, the function skips that version and counts it as already waiting. The other stalled versions are
still deferred. A job that is already running does not block the lock, and that is safe: two running attempts cannot
both write.

The sweep logs counts only. It never logs an id, a title or a filename.
"""

import logging
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

_ENQUEUE: Final = text(
    "SELECT out_enqueued, out_already_waiting FROM grounded.enqueue_stalled_ingest(:lease_seconds)"
)


def ingest_lock(version_id: object) -> str:
    """The queueing lock for one version's ingest job. The function builds the same text in SQL (ADR 0023)."""
    return f"ingest:{version_id}"


async def sweep_stalled_versions(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    lease_seconds: int,
) -> tuple[int, int]:
    """Defer one ingest job for each stalled version. Returns (enqueued, already_waiting)."""
    async with session_factory() as session:
        row = (await session.execute(_ENQUEUE, {"lease_seconds": lease_seconds})).one()
        await session.commit()
    enqueued, already_waiting = int(row[0]), int(row[1])
    logger.info("ingest.stalled_sweep enqueued=%d already_waiting=%d", enqueued, already_waiting)
    return enqueued, already_waiting

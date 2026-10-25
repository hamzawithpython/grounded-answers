"""
Queue cleanup (ADR 0028, amends ADR 0015 decision 5).

A daily periodic task calls the definer grounded.cleanup_finished_jobs (migration 0015) with the retention window. It
deletes succeeded jobs whose last event is older than the window. Nothing else is deleted: queued, running, retrying,
failed, cancelled and aborted jobs stay. Failed jobs are kept for diagnosis.

The count is logged. Job ids and arguments are never logged.
"""

import logging
from typing import Final

import procrastinate
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

QUEUE_CLEANUP_TASK: Final = "grounded.queue_cleanup"
# A succeeded job is kept for one week after its last event (ADR 0028).
QUEUE_RETENTION_HOURS: Final = 168
# Every day at 03:00 UTC.
QUEUE_CLEANUP_CRON: Final = "0 3 * * *"

_CLEANUP: Final = text("SELECT grounded.cleanup_finished_jobs(:retention_hours)")


async def cleanup_finished_jobs(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    retention_hours: int = QUEUE_RETENTION_HOURS,
) -> int:
    """Delete succeeded jobs older than the window. Returns how many were removed."""
    async with session_factory() as session:
        removed = (
            await session.execute(_CLEANUP, {"retention_hours": retention_hours})
        ).scalar_one()
        await session.commit()
    logger.info("queue.cleanup removed=%d retention_hours=%d", int(removed), retention_hours)
    return int(removed)


def register_queue_cleanup(
    app: procrastinate.App, *, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Register the daily cleanup on the worker's app."""

    @app.periodic(cron=QUEUE_CLEANUP_CRON)
    @app.task(name=QUEUE_CLEANUP_TASK)
    async def queue_cleanup(timestamp: int) -> None:
        await cleanup_finished_jobs(session_factory=session_factory)

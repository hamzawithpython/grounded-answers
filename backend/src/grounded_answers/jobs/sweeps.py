"""
The worker's sweeps, run by an in-process timer (ADR 0023, ADR 0027, ADR 0028, amended in PR 9).

The sweeps used to be Procrastinate periodic tasks. Procrastinate's periodic deferrer runs as the worker's own role and
defers jobs with any task name, so it needs rights that worker_user must not have (PR 3's decision). So each sweep is now
a plain call to its existing narrow definer, made by a timer in this process:

- stalled-version retry: grounded.enqueue_stalled_ingest (every 5 minutes),
- purge: grounded.enqueue_purge_jobs (every 15 minutes),
- queue cleanup: grounded.cleanup_finished_jobs (every 24 hours, counted from the start of the worker).

Rules, each one a test:
- a sweep runs once when the worker starts, then on its interval;
- a sweep never overlaps itself: a tick that arrives while the previous run is still going is skipped, and logged;
- a failure is caught and logged with the exception's class name only. The message is not logged, because it can carry
  ids. The timer keeps running. Consecutive failures are counted, and the third and later ones log at warning level;
- a success resets the count.

The sweeps are idempotent and state-based, so a restart simply runs them again, and two worker processes both sweeping
is harmless (queueing locks and the definers' own checks dedupe).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from grounded_answers.jobs.queue_cleanup import QUEUE_RETENTION_HOURS, cleanup_finished_jobs
from grounded_answers.jobs.stalled import sweep_stalled_versions
from grounded_answers.purge import sweep_purge

logger = logging.getLogger(__name__)

# The number of consecutive failures at which a sweep's failure is logged as a warning, not an error.
REPEATED_FAILURE_THRESHOLD: Final = 3


@dataclass(frozen=True)
class SweepSchedule:
    """The interval of each sweep, in seconds. The production values are the ones in the module docstring."""

    stalled_seconds: float = 5 * 60
    purge_seconds: float = 15 * 60
    queue_cleanup_seconds: float = 24 * 60 * 60


@dataclass(frozen=True)
class Sweep:
    name: str
    interval_seconds: float
    run: Callable[[], Awaitable[object]]


def build_sweeps(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    lease_seconds: int,
    schedule: SweepSchedule,
) -> list[Sweep]:
    """The three sweeps, each bound to its definer's call."""

    async def stalled() -> object:
        return await sweep_stalled_versions(
            session_factory=session_factory, lease_seconds=lease_seconds
        )

    async def purge() -> object:
        return await sweep_purge(session_factory=session_factory)

    async def cleanup() -> object:
        return await cleanup_finished_jobs(
            session_factory=session_factory, retention_hours=QUEUE_RETENTION_HOURS
        )

    return [
        Sweep("stalled_retry", schedule.stalled_seconds, stalled),
        Sweep("purge", schedule.purge_seconds, purge),
        Sweep("queue_cleanup", schedule.queue_cleanup_seconds, cleanup),
    ]


class SweepTimer:
    """Runs one sweep on its interval, never overlapping itself, and survives its failures."""

    def __init__(self, sweep: Sweep) -> None:
        self._sweep = sweep
        self._current: asyncio.Task[None] | None = None
        self._consecutive_failures = 0

    async def run_forever(self) -> None:
        self._tick()
        while True:
            await asyncio.sleep(self._sweep.interval_seconds)
            self._tick()

    def _tick(self) -> None:
        if self._current is not None and not self._current.done():
            logger.info("sweep.skipped name=%s reason=previous_run_still_going", self._sweep.name)
            return
        self._current = asyncio.create_task(self._run_once())

    async def _run_once(self) -> None:
        try:
            await self._sweep.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._consecutive_failures += 1
            # The class name only: a message can carry an id or a driver detail.
            if self._consecutive_failures >= REPEATED_FAILURE_THRESHOLD:
                logger.warning(
                    "sweep.repeated_failure name=%s consecutive=%d error=%s",
                    self._sweep.name,
                    self._consecutive_failures,
                    type(exc).__name__,
                )
            else:
                logger.error("sweep.failed name=%s error=%s", self._sweep.name, type(exc).__name__)
            return
        self._consecutive_failures = 0

    async def cancel(self) -> None:
        if self._current is not None:
            self._current.cancel()


def start_sweeps(sweeps: Sequence[Sweep]) -> list[tuple[SweepTimer, asyncio.Task[None]]]:
    """Start one timer per sweep. Returns the timers and their tasks, so the worker can stop them."""
    started: list[tuple[SweepTimer, asyncio.Task[None]]] = []
    for sweep in sweeps:
        timer = SweepTimer(sweep)
        started.append(
            (timer, asyncio.create_task(timer.run_forever(), name=f"sweep-{sweep.name}"))
        )
    return started


async def stop_sweeps(started: Sequence[tuple[SweepTimer, asyncio.Task[None]]]) -> None:
    """Cancel every timer and any run in progress, and wait for them to finish."""
    for timer, _task in started:
        await timer.cancel()
    for _timer, task in started:
        task.cancel()
    await asyncio.gather(*(task for _timer, task in started), return_exceptions=True)

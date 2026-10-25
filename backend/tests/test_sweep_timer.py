"""The worker's sweep timer (jobs/sweeps.py): its rules, without a database."""

import asyncio
import contextlib
import logging

import pytest
from _pytest.logging import LogCaptureFixture

from grounded_answers.jobs.sweeps import (
    REPEATED_FAILURE_THRESHOLD,
    Sweep,
    SweepSchedule,
    SweepTimer,
    start_sweeps,
    stop_sweeps,
)


def _messages(caplog: LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_the_production_schedule_is_five_minutes_fifteen_and_a_day() -> None:
    schedule = SweepSchedule()
    assert schedule.stalled_seconds == 5 * 60
    assert schedule.purge_seconds == 15 * 60
    assert schedule.queue_cleanup_seconds == 24 * 60 * 60


@pytest.mark.anyio
async def test_a_sweep_runs_at_start_and_then_on_its_interval() -> None:
    calls: list[int] = []

    async def run() -> None:
        calls.append(1)

    timer = SweepTimer(Sweep("tick", 0.05, run))
    task = asyncio.create_task(timer.run_forever())
    await asyncio.sleep(0.23)
    await _stop(task)
    assert len(calls) >= 3, "one run at start, then one per interval"


@pytest.mark.anyio
async def test_a_tick_that_arrives_during_a_run_is_skipped_and_logged(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    gate = asyncio.Event()
    started: list[int] = []

    async def run() -> None:
        started.append(1)
        await gate.wait()

    timer = SweepTimer(Sweep("slow", 0.01, run))
    task = asyncio.create_task(timer.run_forever())
    await asyncio.sleep(0.2)  # many ticks, all while the first run is blocked
    assert len(started) == 1, "a sweep never overlaps itself"
    assert "sweep.skipped name=slow reason=previous_run_still_going" in _messages(caplog)
    gate.set()
    await _stop(task)


@pytest.mark.anyio
async def test_a_failure_is_logged_without_its_message_and_the_timer_continues(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    calls: list[int] = []

    async def run() -> None:
        calls.append(1)
        raise RuntimeError("SECRET-ID-7f3a9 a document detail that must not be logged")

    timer = SweepTimer(Sweep("broken", 0.02, run))
    task = asyncio.create_task(timer.run_forever())
    await asyncio.sleep(0.15)
    await _stop(task)

    messages = _messages(caplog)
    assert len(calls) >= 2, "the timer keeps running after a failure"
    assert "sweep.failed name=broken error=RuntimeError" in messages
    assert not any("SECRET-ID" in message for message in messages), "the message must not be logged"


@pytest.mark.anyio
async def test_repeated_failures_warn_and_a_success_resets_the_count(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    outcomes: list[bool] = [False] * REPEATED_FAILURE_THRESHOLD + [True, False]
    remaining = iter(outcomes)

    async def run() -> None:
        if next(remaining):
            return
        raise RuntimeError("boom")

    timer = SweepTimer(Sweep("flaky", 60, run))
    for _ in outcomes:
        await timer._run_once()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [w.getMessage() for w in warnings] == [
        f"sweep.repeated_failure name=flaky consecutive={REPEATED_FAILURE_THRESHOLD} error=RuntimeError"
    ]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 3, (
        "two errors before the threshold, one after the reset; the third failure warns"
    )


@pytest.mark.anyio
async def test_stopping_the_timers_cancels_their_runs() -> None:
    gate = asyncio.Event()

    async def run() -> None:
        await gate.wait()

    started = start_sweeps([Sweep("stuck", 0.01, run)])
    await asyncio.sleep(0.05)
    await asyncio.wait_for(stop_sweeps(started), timeout=2)
    assert all(task.done() for _timer, task in started)

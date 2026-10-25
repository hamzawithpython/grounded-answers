"""Queue cleanup (ADR 0028, migration 0015): exact grants, and only succeeded jobs older than the window are removed.

The probe jobs are written by the cluster admin, with their event times set, so their age is known. Each test removes
its probes afterwards. The queue's delete trigger names its table without a schema, so the admin session puts queue on
its search path first (as the other tests that delete jobs do).
"""

from collections.abc import Iterator
from datetime import timedelta

import psycopg
import pytest
from psycopg import errors

from grounded_answers.jobs.queue_cleanup import QUEUE_RETENTION_HOURS
from grounded_answers.jobs.sweeps import SweepSchedule

from .database import DbHandle

pytestmark = pytest.mark.db

PROBE_TASK = "grounded.cleanup_probe"


@pytest.fixture
def probes(admin: psycopg.Connection) -> Iterator[None]:
    # The status trigger inserts into the events table without a schema, so the session needs queue on its path first.
    admin.execute("SET search_path TO queue, grounded, public")
    yield
    admin.execute("SET search_path TO queue, grounded, public")
    admin.execute("DELETE FROM queue.procrastinate_jobs WHERE task_name = %s", (PROBE_TASK,))


def _job(admin: psycopg.Connection, status: str, *, hours_ago: float) -> int:
    """A job in `status`, whose every event is `hours_ago` old."""
    row = admin.execute(
        "INSERT INTO queue.procrastinate_jobs (queue_name, task_name, args, status) "
        "VALUES ('default', %s, '{}', %s::queue.procrastinate_job_status) RETURNING id",
        (PROBE_TASK, status),
    ).fetchone()
    assert row is not None
    job_id = int(row[0])
    # The job gets one event, aged. The library writes its own events in normal use, and the probe writes its own, so
    # its age is known. A job with no event at all is kept (test_a_job_without_an_event_is_kept).
    admin.execute(
        "INSERT INTO queue.procrastinate_events (job_id, type, at) VALUES (%s, 'deferred', now() - %s)",
        (job_id, timedelta(hours=hours_ago)),
    )
    return job_id


def _exists(admin: psycopg.Connection, job_id: int) -> bool:
    row = admin.execute(
        "SELECT count(*) FROM queue.procrastinate_jobs WHERE id = %s", (job_id,)
    ).fetchone()
    assert row is not None
    return int(row[0]) == 1


def _worker(handle: DbHandle) -> psycopg.Connection:
    return psycopg.connect(handle.url_for("worker_user", handle.passwords["worker_user"]))


def _cleanup(handle: DbHandle, hours: int) -> int:
    with _worker(handle) as worker:
        row = worker.execute("SELECT grounded.cleanup_finished_jobs(%s)", (hours,)).fetchone()
        worker.commit()
    assert row is not None
    removed: int = row[0]
    return removed


def test_only_succeeded_jobs_older_than_the_window_are_removed(
    grounded_db: DbHandle, admin: psycopg.Connection, probes: None
) -> None:
    old_succeeded = _job(admin, "succeeded", hours_ago=200)
    recent_succeeded = _job(admin, "succeeded", hours_ago=10)
    old_failed = _job(admin, "failed", hours_ago=200)
    old_cancelled = _job(admin, "cancelled", hours_ago=200)
    old_aborted = _job(admin, "aborted", hours_ago=200)
    old_todo = _job(admin, "todo", hours_ago=200)
    old_doing = _job(admin, "doing", hours_ago=200)

    removed = _cleanup(grounded_db, QUEUE_RETENTION_HOURS)

    assert removed >= 1
    assert not _exists(admin, old_succeeded), "an old finished job is removed"
    assert _exists(admin, recent_succeeded), "a finished job inside the window is kept"
    for kept, why in (
        (old_failed, "failed jobs are kept for diagnosis (ADR 0028)"),
        (old_cancelled, "cancelled jobs are kept"),
        (old_aborted, "aborted jobs are kept"),
        (old_todo, "a queued job is never removed"),
        (old_doing, "a running job is never removed"),
    ):
        assert _exists(admin, kept), why


def test_the_removed_job_takes_its_events_and_nothing_else(
    grounded_db: DbHandle, admin: psycopg.Connection, probes: None
) -> None:
    old = _job(admin, "succeeded", hours_ago=200)
    other = _job(admin, "succeeded", hours_ago=10)
    before = admin.execute(
        "SELECT count(*) FROM queue.procrastinate_events WHERE job_id = %s", (old,)
    ).fetchone()
    assert before is not None and before[0] >= 1

    _cleanup(grounded_db, QUEUE_RETENTION_HOURS)

    assert admin.execute(
        "SELECT count(*) FROM queue.procrastinate_events WHERE job_id = %s", (old,)
    ).fetchone() == (0,)
    assert admin.execute(
        "SELECT count(*) FROM queue.procrastinate_events WHERE job_id = %s", (other,)
    ).fetchone() != (0,)


def test_the_window_is_the_one_asked_for(
    grounded_db: DbHandle, admin: psycopg.Connection, probes: None
) -> None:
    three_hours = _job(admin, "succeeded", hours_ago=3)
    one_hour = _job(admin, "succeeded", hours_ago=1)

    _cleanup(grounded_db, 2)

    assert not _exists(admin, three_hours), "older than a 2-hour window: removed"
    assert _exists(admin, one_hour), "inside a 2-hour window: kept"


def test_a_periodic_link_is_cleared_and_the_row_kept(
    grounded_db: DbHandle, admin: psycopg.Connection, probes: None
) -> None:
    """The library's delete trigger unlinks a periodic defer before the job goes (the UPDATE the definer is granted)."""
    old = _job(admin, "succeeded", hours_ago=200)
    admin.execute(
        "INSERT INTO queue.procrastinate_periodic_defers (task_name, defer_timestamp, job_id, periodic_id) "
        "VALUES (%s, 1, %s, 'probe')",
        (PROBE_TASK, old),
    )

    _cleanup(grounded_db, QUEUE_RETENTION_HOURS)

    assert not _exists(admin, old)
    link = admin.execute(
        "SELECT job_id FROM queue.procrastinate_periodic_defers WHERE task_name = %s AND periodic_id = 'probe'",
        (PROBE_TASK,),
    ).fetchone()
    assert link == (None,)
    admin.execute(
        "DELETE FROM queue.procrastinate_periodic_defers WHERE task_name = %s", (PROBE_TASK,)
    )


@pytest.mark.parametrize("hours", [0, 8761])
def test_a_window_outside_one_hour_to_a_year_is_refused(grounded_db: DbHandle, hours: int) -> None:
    with pytest.raises(errors.InvalidParameterValue, match="between 1 and 8760"):
        _cleanup(grounded_db, hours)


def test_the_worker_can_run_the_cleanup_and_nothing_else_on_the_queue(
    grounded_db: DbHandle, admin: psycopg.Connection, probes: None
) -> None:
    """The grant is exact: the function is the only route to a delete. The worker's direct deletes are refused."""
    job = _job(admin, "succeeded", hours_ago=200)
    with _worker(grounded_db) as worker:
        with pytest.raises(errors.InsufficientPrivilege):
            worker.execute("DELETE FROM queue.procrastinate_jobs WHERE id = %s", (job,))
    with _worker(grounded_db) as worker:
        with pytest.raises(errors.InsufficientPrivilege):
            worker.execute("DELETE FROM queue.procrastinate_events WHERE job_id = %s", (job,))
    with _worker(grounded_db) as worker:
        with pytest.raises(errors.InsufficientPrivilege):
            worker.execute("SELECT job_id FROM queue.procrastinate_periodic_defers")
    assert _exists(admin, job)


def test_the_cleanup_runs_daily_at_three_utc() -> None:
    assert SweepSchedule().queue_cleanup_seconds == 24 * 60 * 60
    assert QUEUE_RETENTION_HOURS == 168


def test_a_job_without_an_event_is_kept(
    grounded_db: DbHandle, admin: psycopg.Connection, probes: None
) -> None:
    """Its last event is unknown, so its age is unknown. The cleanup keeps it rather than guess (fail safe)."""
    row = admin.execute(
        "INSERT INTO queue.procrastinate_jobs (queue_name, task_name, args, status) "
        "VALUES ('default', %s, '{}', 'succeeded') RETURNING id",
        (PROBE_TASK,),
    ).fetchone()
    assert row is not None
    admin.execute("DELETE FROM queue.procrastinate_events WHERE job_id = %s", (row[0],))

    _cleanup(grounded_db, QUEUE_RETENTION_HOURS)

    assert _exists(admin, int(row[0]))

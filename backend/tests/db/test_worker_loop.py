"""The REAL worker loop, as worker_user, against the test database (PR 9's integration test).

This is the test the periodic-task bug would have failed: it runs serve() (the production loop: login check, Procrastinate's
worker, the sweep timers) for a few seconds, and checks that every sweep ran and that nothing failed with a permission
error. The embedder is a stand-in, so the 1.1 GB model is not needed here; main() is tested separately
(tests/test_worker_main_loads_model.py).
"""

import asyncio
import logging
from collections.abc import Callable

import procrastinate
import psycopg
import pytest
from _pytest.logging import LogCaptureFixture

from grounded_answers.jobs import worker
from grounded_answers.jobs.sweeps import SweepSchedule

from .database import DbHandle
from .ingest_support import FakeEmbedder

pytestmark = pytest.mark.db

# Short intervals, so each sweep fires within the test. The production values are tested in test_sweep_timer.py.
FAST = SweepSchedule(stalled_seconds=0.2, purge_seconds=0.2, queue_cleanup_seconds=0.2)
RUN_SECONDS = 4.0


async def _run_worker(
    handle: DbHandle, configure: Callable[[procrastinate.App], None] | None = None
) -> None:
    conninfo = handle.url_for("worker_user", handle.passwords["worker_user"])
    try:
        await asyncio.wait_for(
            worker.serve(
                conninfo=conninfo,
                database_url=conninfo.replace("postgresql://", "postgresql+psycopg://", 1),
                lease_seconds=120,
                retention_days=30,
                embedder=FakeEmbedder(),
                schedule=FAST,
                install_signal_handlers=False,
                configure=configure,
            ),
            timeout=RUN_SECONDS,
        )
    except TimeoutError:
        pass  # the worker runs until it is stopped; the timeout is how this test stops it


@pytest.mark.anyio
async def test_the_real_worker_loop_runs_every_sweep_as_worker_user(
    grounded_db: DbHandle, admin: psycopg.Connection, caplog: LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    # Other tests leave queued jobs for tasks this worker does not register (for example grounded.enqueue_check). The
    # worker would log them as errors, so the test starts from an empty queue. This is the test database only.
    admin.execute("SET search_path TO queue, grounded, public")
    admin.execute("DELETE FROM queue.procrastinate_jobs WHERE status = 'todo'")

    await _run_worker(grounded_db)

    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("ingest.stalled_sweep ") for m in messages), (
        "the stalled-retry sweep never ran"
    )
    assert any(m.startswith("purge.sweep ") for m in messages), "the purge sweep never ran"
    assert any(m.startswith("queue.cleanup ") for m in messages), "the queue cleanup never ran"
    assert not any("permission denied" in m for m in messages), "a permission error reached the log"
    assert not any("deferrer failed" in m.lower() for m in messages), "the periodic deferrer failed"
    assert not any("sweep.failed" in m or "sweep.repeated_failure" in m for m in messages)
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], f"the worker logged errors: {errors}"


@pytest.mark.anyio
async def test_worker_user_has_no_execute_on_any_periodic_defer_function(
    grounded_db: DbHandle, admin: psycopg.Connection
) -> None:
    rows = admin.execute(
        "SELECT p.oid FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'queue' AND p.proname LIKE 'procrastinate_defer_periodic%'"
    ).fetchall()
    assert rows, "the check must find the periodic defer functions, or it proves nothing"
    for (oid,) in rows:
        granted = admin.execute(
            "SELECT has_function_privilege('worker_user', %s::oid, 'EXECUTE')", (oid,)
        ).fetchone()
        assert granted == (False,), f"worker_user can execute a periodic defer function (oid {oid})"

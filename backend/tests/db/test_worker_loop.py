"""The REAL worker loop, as worker_user, against the test database (PR 9's integration test).

This is the test the periodic-task bug would have failed. It runs serve() (the production loop: login check, Procrastinate's
worker, the sweep timers) with one real ingest job queued, and checks that the startup config is logged, that every sweep
ran, that the ingest job reached ready with its success line, and that nothing failed with a permission error. The
embedder is a stand-in, so the 1.1 GB model is not needed here; main() is tested separately
(tests/test_worker_main_loads_model.py), and the startup lines of the real process are checked in
tests/db/test_worker_startup_logs.py.
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
from .ingest_support import FakeEmbedder, Made, enqueue_ingest, make_version, version_row
from .seed import Seeded

pytestmark = pytest.mark.db

# Short intervals, so each sweep fires within the test. The production values are tested in test_sweep_timer.py.
FAST = SweepSchedule(stalled_seconds=0.2, purge_seconds=0.2, queue_cleanup_seconds=0.2)
# A ceiling only: the run stops as soon as every expected line is logged.
RUN_SECONDS = 60.0
POLICY_MD = (
    b"# 3 Probation\n\n"
    b"3.2 Probation is six months for LTV drivers.\n\n"
    b"3.2.2 A road assessment is due before the end of month four.\n"
)
# Every line this run must log. Prefixes, so the values in each line can vary.
EXPECTED_LINES = (
    "worker.config document_retention_days=30",
    "ingest.stalled_sweep ",
    "purge.sweep ",
    "queue.cleanup ",
    "ingest.ready chunks=",
)


def _messages(caplog: LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def _all_expected_logged(caplog: LogCaptureFixture) -> bool:
    messages = _messages(caplog)
    return all(any(m.startswith(prefix) for m in messages) for prefix in EXPECTED_LINES)


async def _run_worker(
    handle: DbHandle,
    caplog: LogCaptureFixture,
    configure: Callable[[procrastinate.App], None] | None = None,
) -> None:
    conninfo = handle.url_for("worker_user", handle.passwords["worker_user"])
    serve_task = asyncio.create_task(
        worker.serve(
            conninfo=conninfo,
            database_url=conninfo.replace("postgresql://", "postgresql+psycopg://", 1),
            lease_seconds=120,
            retention_days=30,
            embedder=FakeEmbedder(),
            schedule=FAST,
            install_signal_handlers=False,
            configure=configure,
        )
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RUN_SECONDS
    try:
        while not serve_task.done() and not _all_expected_logged(caplog) and loop.time() < deadline:
            await asyncio.sleep(0.1)
    finally:
        # The worker runs until it is stopped; cancelling it is how this test stops it.
        serve_task.cancel()
        await asyncio.gather(serve_task, return_exceptions=True)
    if not serve_task.cancelled():
        failure = serve_task.exception()
        if failure is not None:
            raise failure


@pytest.mark.anyio
async def test_the_real_worker_loop_runs_every_sweep_and_an_ingest_job_as_worker_user(
    grounded_db: DbHandle,
    seeded: Seeded,
    made: Made,
    admin: psycopg.Connection,
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    # Other tests leave queued jobs for tasks this worker does not register (for example grounded.enqueue_check). The
    # worker would log them as errors, so the test starts from an empty queue. This is the test database only.
    admin.execute("SET search_path TO queue, grounded, public")
    admin.execute("DELETE FROM queue.procrastinate_jobs WHERE status = 'todo'")
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type="text/markdown", data=POLICY_MD
    )
    await enqueue_ingest(grounded_db, seeded.a.tenant, document, version)

    await _run_worker(grounded_db, caplog)

    messages = _messages(caplog)
    for prefix in EXPECTED_LINES:
        assert any(m.startswith(prefix) for m in messages), (
            f"the worker never logged a line starting with {prefix!r}"
        )
    assert version_row(admin, version)[0] == "ready", (
        "the ingest job did not make the version ready"
    )
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

"""The worker's startup log, through the real startup path (PR 9 follow-up).

The worker is started as a separate process, exactly as an operator starts it (python -m grounded_answers.jobs.worker),
against the test database, as worker_user, with the real model. One real ingest job is queued first, so the pipeline's
success line is produced by the process under test. The test reads its output until every startup line has appeared,
then stops the process it started. This is the path where two bugs hid: a logger named __main__, and a logger that no
handler covered. Unit tests import the module instead, so they could not see either.
"""

import asyncio
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg
import pytest

from grounded_answers.runtime import selector_loop_factory

from .database import DbHandle
from .ingest_support import Made, enqueue_ingest, make_version, version_row
from .seed import Seeded

pytestmark = pytest.mark.db

BACKEND = Path(__file__).resolve().parents[2]
POLICY_MD = (
    b"# 3 Probation\n\n"
    b"3.2 Probation is six months for LTV drivers.\n\n"
    b"3.2.2 A road assessment is due before the end of month four.\n"
)
STARTUP_LINES: dict[str, str] = {
    "retention": r"worker\.config document_retention_days=30",
    "model": r"worker\.model_loaded",
    "stalled sweep": r"ingest\.stalled_sweep enqueued=\d+ already_waiting=\d+",
    "purge sweep": r"purge\.sweep enqueued=\d+ already_waiting=\d+",
    "queue cleanup": r"queue\.cleanup removed=\d+ retention_hours=\d+",
    "ingest success": r"ingest\.ready chunks=\d+ swap=\S+",
}
STARTUP_TIMEOUT_SECONDS = 300


def test_the_real_worker_process_logs_every_startup_line(
    grounded_db: DbHandle, seeded: Seeded, made: Made, admin: psycopg.Connection
) -> None:
    # Read into a variable first: a failing assert on os.environ would print every variable, the admin password included.
    model_dir = os.environ.get("TEST_EMBEDDING_MODEL_DIR")
    assert model_dir, (
        "TEST_EMBEDDING_MODEL_DIR is not set. This test needs the real model "
        "(docs/RUNBOOK.md, Embedding model files)"
    )
    # Other tests leave queued jobs for tasks this worker does not register. The test database only.
    admin.execute("SET search_path TO queue, grounded, public")
    admin.execute("DELETE FROM queue.procrastinate_jobs WHERE status = 'todo'")
    document, version = make_version(
        admin, made, seeded.a.tenant, seeded.a.admin, content_type="text/markdown", data=POLICY_MD
    )
    asyncio.run(
        enqueue_ingest(grounded_db, seeded.a.tenant, document, version),
        loop_factory=selector_loop_factory,
    )

    env = dict(os.environ)
    env["WORKER_DATABASE_URL"] = grounded_db.url_for(
        "worker_user", grounded_db.passwords["worker_user"]
    )
    env["WORKER_EMBEDDING_MODEL_DIR"] = model_dir
    env["DOCUMENT_RETENTION_DAYS"] = "30"
    process = subprocess.Popen(
        [sys.executable, "-m", "grounded_answers.jobs.worker"],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: list[str] = []

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line.rstrip())

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        missing = dict(STARTUP_LINES)
        while missing and time.monotonic() < deadline and process.poll() is None:
            text = "\n".join(lines)
            missing = {
                name: pattern for name, pattern in missing.items() if not re.search(pattern, text)
            }
            time.sleep(0.5)
    finally:
        process.terminate()  # the process this test started, by its handle
        process.wait(timeout=60)
        reader.join(timeout=10)

    output = "\n".join(lines)
    assert not missing, (
        f"the worker did not log at startup: {sorted(missing)}\n--- worker output ---\n{output[-4000:]}"
    )
    assert version_row(admin, version)[0] == "ready", (
        "the ingest job did not make the version ready"
    )
    assert "permission denied" not in output, "a permission error at startup"
    assert "Side task deferrer failed" not in output, "the periodic deferrer failed at startup"
    assert "sweep.failed" not in output and "sweep.repeated_failure" not in output, (
        "a sweep failed at startup"
    )

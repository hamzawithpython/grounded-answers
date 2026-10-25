"""
The worker process (ADR 0015 decision 8, ADR 0017, ADR 0022).

Start it from backend/, with the worker settings set in the shell (WORKER_DATABASE_URL and
WORKER_EMBEDDING_MODEL_DIR, see docs/RUNBOOK.md):

    uv run python -m grounded_answers.jobs.worker

It logs in as worker_user. It uses the selector event loop, as the API does, because psycopg's async driver
needs it on Windows. The model is loaded once, here at startup, and the worker refuses to start if the model
files are missing or do not match their pins. It also checks the database login once, at startup, so a wrong
password stops the worker with a clear message instead of a retry loop. Windows cannot stop the worker
gracefully (upstream limit, ADR 0015): stop it by its process ID. That is safe, because delivery is at least
once and every job is idempotent.

The three sweeps run in this process, on a timer (jobs/sweeps.py, ADR 0023, ADR 0027, ADR 0028). They are plain calls to
their definers, so the worker needs no Procrastinate periodic rights.

The keyword arguments of serve() other than the settings exist for the tests (embedder, schedule, configure,
install_signal_handlers). main() passes none of them, so production always loads the real model.
"""

import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import procrastinate
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from grounded_answers.database import create_database_engine, create_session_factory
from grounded_answers.embedding.model import Embedder, ModelIntegrityError, load_embedder
from grounded_answers.jobs.app import build_app
from grounded_answers.jobs.ingest_task import register_ingest_task
from grounded_answers.jobs.logging_filter import install_job_log_redaction
from grounded_answers.jobs.sweeps import SweepSchedule, build_sweeps, start_sweeps, stop_sweeps
from grounded_answers.purge import register_purge_task
from grounded_answers.runtime import selector_loop_factory
from grounded_answers.storage.postgres_store import PostgresObjectStore
from grounded_answers.worker_config import WorkerSettings

# Not __name__: the worker runs as "python -m grounded_answers.jobs.worker", where __name__ is "__main__", and no
# handler covers that name. The explicit name keeps these lines under grounded_answers.jobs (ADR 0017).
logger = logging.getLogger("grounded_answers.jobs.worker")

LOGIN_CHECK_TIMEOUT_SECONDS: Final = 10
# A failed login is raised by the connection step, so psycopg attaches no SQLSTATE. The server's wording is the signal.
AUTH_FAILURE_TEXT: Final = "password authentication failed"


class WorkerLoginFailed(RuntimeError):
    """The worker's database login was refused. The message names the role and nothing else: no URL, no password."""


class WorkerDatabaseUnreachable(RuntimeError):
    """The startup check could not reach the database in time. The message holds no detail from the driver."""


async def check_worker_login(database_url: str) -> None:
    """
    Connect once as worker_user and run SELECT 1, with a short timeout (ADR 0022).

    Raises WorkerLoginFailed on a wrong password, and WorkerDatabaseUnreachable on anything else.
    The driver's exception is not chained, so a traceback cannot print the URL or a password.
    """
    engine = create_database_engine(database_url)
    try:
        async with asyncio.timeout(LOGIN_CHECK_TIMEOUT_SECONDS):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except DBAPIError as exc:
        if AUTH_FAILURE_TEXT in str(exc.orig):
            raise WorkerLoginFailed("database login failed for worker_user") from None
        raise WorkerDatabaseUnreachable("database check failed") from None
    except TimeoutError:
        raise WorkerDatabaseUnreachable("database check timed out") from None
    finally:
        await engine.dispose()


def log_configuration(retention_days: int) -> None:
    """One line at startup, so a retention value that differs from the web app's is visible in the worker's log (ADR 0027)."""
    logger.info("worker.config document_retention_days=%d", retention_days)


async def serve(
    *,
    conninfo: str,
    database_url: str,
    lease_seconds: int,
    retention_days: int,
    model_dir: Path | None = None,
    embedder: Embedder | None = None,
    schedule: SweepSchedule | None = None,
    install_signal_handlers: bool = True,
    configure: Callable[[procrastinate.App], None] | None = None,
) -> None:
    """
    Run the worker until it stops. Production passes model_dir and no embedder, so the real model is loaded here.

    `embedder`, `schedule`, `install_signal_handlers` and `configure` are for the tests. `configure` is called with the
    app before the worker runs; the integration test uses it to plant a defect (PR 9's planted failure).
    """
    log_configuration(retention_days)
    if embedder is None:
        if model_dir is None:
            raise ValueError("serve needs a model directory, or an embedder for tests")
        embedder = load_embedder(model_dir)
    await check_worker_login(database_url)
    engine = create_database_engine(database_url)
    session_factory = create_session_factory(engine)
    app = build_app(conninfo)
    register_ingest_task(
        app,
        session_factory=session_factory,
        object_store=PostgresObjectStore(),
        embedder=embedder,
        lease_seconds=lease_seconds,
        retention_days=retention_days,
    )
    register_purge_task(app, session_factory=session_factory)
    if configure is not None:
        configure(app)
    sweeps = build_sweeps(
        session_factory=session_factory,
        lease_seconds=lease_seconds,
        schedule=schedule if schedule is not None else SweepSchedule(),
    )
    started = start_sweeps(sweeps)
    try:
        async with app.open_async():
            await app.run_worker_async(install_signal_handlers=install_signal_handlers)
    finally:
        await stop_sweeps(started)
        await engine.dispose()


def main() -> None:
    settings = WorkerSettings()
    install_job_log_redaction()
    if settings.worker_embedding_model_dir is None:
        sys.exit(
            "WORKER_EMBEDDING_MODEL_DIR is not set. See docs/RUNBOOK.md, 'Embedding model files'."
        )
    conninfo = str(settings.worker_database_url)
    # SQLAlchemy needs the driver in the scheme; Procrastinate takes the plain libpq string.
    database_url = conninfo.replace("postgresql://", "postgresql+psycopg://", 1)
    try:
        asyncio.run(
            serve(
                conninfo=conninfo,
                database_url=database_url,
                model_dir=settings.worker_embedding_model_dir,
                lease_seconds=settings.worker_processing_lease_seconds,
                retention_days=settings.document_retention_days,
            ),
            loop_factory=selector_loop_factory,
        )
    except ModelIntegrityError as exc:
        sys.exit(f"worker not started: {exc}")
    except (WorkerLoginFailed, WorkerDatabaseUnreachable) as exc:
        sys.exit(f"worker not started: {exc}")


if __name__ == "__main__":
    main()

"""
The worker process (ADR 0015 decision 8, ADR 0017, ADR 0022).

Start it from backend/, with the worker settings set in the shell (WORKER_DATABASE_URL and
WORKER_EMBEDDING_MODEL_DIR, see docs/RUNBOOK.md):

    uv run python -m grounded_answers.jobs.worker

It logs in as worker_user. It uses the selector event loop, as the API does, because psycopg's async driver
needs it on Windows. The model is loaded once, here at startup, and the worker refuses to start if the model
files are missing or do not match their pins. Windows cannot stop the worker gracefully (upstream limit,
ADR 0015): stop it by its process ID. That is safe, because delivery is at least once and every job is
idempotent.
"""

import asyncio
import sys
from pathlib import Path

from grounded_answers.database import create_database_engine, create_session_factory
from grounded_answers.embedding.model import FastEmbedder, ModelIntegrityError
from grounded_answers.jobs.app import build_app
from grounded_answers.jobs.ingest_task import register_ingest_task
from grounded_answers.jobs.logging_filter import install_job_log_redaction
from grounded_answers.runtime import selector_loop_factory
from grounded_answers.storage.postgres_store import PostgresObjectStore
from grounded_answers.worker_config import WorkerSettings


async def serve(*, conninfo: str, database_url: str, model_dir: Path, lease_seconds: int) -> None:
    embedder = FastEmbedder(model_dir)
    engine = create_database_engine(database_url)
    session_factory = create_session_factory(engine)
    app = build_app(conninfo)
    register_ingest_task(
        app,
        session_factory=session_factory,
        object_store=PostgresObjectStore(),
        embedder=embedder,
        lease_seconds=lease_seconds,
    )
    try:
        async with app.open_async():
            await app.run_worker_async()
    finally:
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
            ),
            loop_factory=selector_loop_factory,
        )
    except ModelIntegrityError as exc:
        sys.exit(f"worker not started: {exc}")


if __name__ == "__main__":
    main()

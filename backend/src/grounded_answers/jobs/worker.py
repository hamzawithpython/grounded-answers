"""
The worker process (ADR 0015 decision 8, ADR 0017).

Start it from backend/, with WORKER_DATABASE_URL set in the shell:

    uv run python -m grounded_answers.jobs.worker

It logs in as worker_user. It uses the selector event loop, as the API does, because psycopg's
async driver needs it on Windows. Windows cannot stop the worker gracefully (upstream limit,
ADR 0015): stop it by its process ID. That is safe, because delivery is at least once and every
job is idempotent.
"""

import asyncio
import logging

from grounded_answers.jobs.app import build_app
from grounded_answers.jobs.logging_filter import redacted_stream_handler
from grounded_answers.runtime import selector_loop_factory
from grounded_answers.worker_config import WorkerSettings


async def serve(app_conninfo: str) -> None:
    app = build_app(app_conninfo)
    async with app.open_async():
        await app.run_worker_async()


def main() -> None:
    settings = WorkerSettings()
    root = logging.getLogger()
    root.addHandler(redacted_stream_handler())
    root.setLevel(logging.INFO)
    asyncio.run(serve(str(settings.worker_database_url)), loop_factory=selector_loop_factory)


if __name__ == "__main__":
    main()

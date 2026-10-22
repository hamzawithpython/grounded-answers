"""
Enqueue a background job inside the caller's tenant transaction (ADR 0015, ADR 0017).

The job row is written on the session's own driver connection (the outbox pattern, spike proved in
ADR 0015). So the job commits or rolls back together with the write that caused it: a document
never exists without its job, and a job never exists without its document.

Rules, checked before anything is written:
- the task name must be on ALLOWED_TASKS;
- every argument must be a uuid.UUID (ADR 0015 decision 3: ids only, never content);
- argument names must be identifiers.

Errors name the rule, never the value, so a refused argument cannot leak into a log or a traceback.
"""

import uuid
from collections.abc import Mapping

import procrastinate
from sqlalchemy.ext.asyncio import AsyncSession

from grounded_answers.jobs.tasks import INGEST_DOCUMENT

# The task names the web app may enqueue. Ingestion is listed now, so the upload endpoint can
# queue it; the upload is off until the worker runs the task (UPLOAD_ENABLED, ADR 0019 D6).
ALLOWED_TASKS: frozenset[str] = frozenset({INGEST_DOCUMENT})


class TaskNotAllowed(ValueError):
    """The task name is not on ALLOWED_TASKS. Nothing is written."""


class InvalidJobArgument(ValueError):
    """An argument is not a UUID, or its name is not an identifier. Nothing is written."""


def validate_job(task_name: str, arguments: Mapping[str, object]) -> dict[str, str]:
    """Check the task name and every argument. Returns the arguments as UUID strings."""
    if task_name not in ALLOWED_TASKS:
        raise TaskNotAllowed("the task name is not on the allowlist")
    payload: dict[str, str] = {}
    for name, value in arguments.items():
        if not name.isidentifier():
            raise InvalidJobArgument("an argument name is not an identifier")
        if not isinstance(value, uuid.UUID):
            raise InvalidJobArgument(
                f"argument {name!r} must be a UUID, not {type(value).__name__}"
            )
        payload[name] = str(value)
    return payload


async def _driver_connection(session: AsyncSession) -> object:
    """The psycopg connection under the session's transaction (the spike's path, ADR 0015)."""
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    return raw.driver_connection


async def enqueue_job(
    session: AsyncSession,
    app: procrastinate.App,
    task_name: str,
    arguments: Mapping[str, object],
) -> int:
    """
    Enqueue one job on the session's transaction. Returns the job id.

    The caller owns the transaction: commit it to keep the job, roll it back to drop the job.
    """
    payload = validate_job(task_name, arguments)
    connection = await _driver_connection(session)
    deferrer = app.configure_task(name=task_name, connection=connection)
    job_id = await deferrer.defer_async(**payload)
    return int(job_id)

"""
Procrastinate's connector finds its tables through search_path (ADR 0016).

Procrastinate's SQL names its tables and functions without a schema, so the role's search_path
decides where they are found. The bootstrap gives app_user and worker_user `grounded, queue`.
These tests prove both halves: the connector sees the queue with `queue` on the path, and does
not see it without the path. The second test is the failure we would get from a bad bootstrap.
"""

import uuid
from urllib.parse import quote

import psycopg
import pytest
from procrastinate import App, PsycopgConnector

from .database import DbHandle

pytestmark = pytest.mark.db


def _role_url(grounded_db: DbHandle, role: str) -> str:
    return grounded_db.url_for(role, grounded_db.passwords[role])


@pytest.mark.anyio
@pytest.mark.parametrize("role", ["app_user", "worker_user"])
async def test_connector_finds_the_queue_with_the_bootstrap_search_path(
    grounded_db: DbHandle, role: str
) -> None:
    """With the bootstrap's search_path, the connector sees procrastinate_jobs."""
    app = App(connector=PsycopgConnector(conninfo=_role_url(grounded_db, role)))
    async with app.open_async() as open_app:
        assert await open_app.job_manager.check_connection_async() is True


@pytest.mark.anyio
async def test_connector_does_not_find_the_queue_without_the_queue_schema_on_the_path(
    grounded_db: DbHandle,
) -> None:
    """Without queue on the path, the same connector reports the table missing (not an error)."""
    search_path = quote("-csearch_path=grounded", safe="")
    url = f"{_role_url(grounded_db, 'worker_user')}?options={search_path}"
    app = App(connector=PsycopgConnector(conninfo=url))
    async with app.open_async() as open_app:
        assert await open_app.job_manager.check_connection_async() is False


@pytest.mark.anyio
async def test_app_user_can_enqueue_one_job_with_one_event_and_cannot_read_arguments(
    grounded_db: DbHandle,
) -> None:
    """Enqueue works as app_user (ADR 0016); app_user cannot read the job's arguments back."""
    url = _role_url(grounded_db, "app_user")
    app = App(connector=PsycopgConnector(conninfo=url))
    document_id = str(uuid.uuid4())
    async with app.open_async() as open_app:
        task = open_app.configure_task(name="grounded.enqueue_check")
        job_id = await task.defer_async(document_id=document_id)

    with psycopg.connect(grounded_db.admin_test_url()) as admin:
        events = admin.execute(
            "SELECT count(*) FROM queue.procrastinate_events WHERE job_id = %s", (job_id,)
        ).fetchone()
    assert events == (1,), "the status trigger should write one 'deferred' event per job"

    with psycopg.connect(url) as app_conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("SELECT args FROM procrastinate_jobs WHERE id = %s", (job_id,))

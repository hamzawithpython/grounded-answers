"""DELETE /documents/{id} (ADR 0025), over HTTP against PostgreSQL, with the guard from migration 0012.

- Retrieval drops the document's chunks as soon as the delete commits (the app_user chunk policy).
- An employee is refused. Another tenant's admin gets the same 404 as for an unknown id, and nothing changes.
- A second delete answers 404, the same body as an unknown id (see the handler's docstring for why).
- Every live version is retired, and the retired one keeps its retirement time.
- A worker that is embedding a version when the delete commits loses cleanly: its finish writes nothing.
- app_user cannot restore a deleted document. The delete path itself is allowed.

TEMPORARY_RETENTION_DAYS (30) stands in for DOCUMENT_RETENTION_DAYS until PR 7 adds the setting.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors

from grounded_answers.database import create_database_engine, create_session_factory
from grounded_answers.documents_api import retire_document
from grounded_answers.embedding.pinned import MODEL_NAME, MODEL_REVISION
from grounded_answers.ingest.pipeline import IngestJob, Outcome, run_ingestion
from grounded_answers.models import EMBEDDING_DIMENSIONS
from grounded_answers.passwords import hash_password
from grounded_answers.storage.postgres_store import PostgresObjectStore
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction

from .auth_support import Accounts
from .database import DbHandle
from .ingest_support import (
    FakeEmbedder,
    Made,
    chunk_count,
    make_version,
    ready_audit_count,
    version_row,
    worker_sessions,
)
from .seed import Seeded

TEMPORARY_RETENTION_DAYS = (
    30  # the default of DOCUMENT_RETENTION_DAYS, used here as the expected period
)

pytestmark = pytest.mark.db

MARKDOWN = "text/markdown"
POLICY_MD = b"# 3 Probation\n\n3.2 Probation is six months for LTV drivers.\n"
NOT_FOUND = {"detail": "Not Found"}


def _vector() -> str:
    return "[" + ",".join("0.1" for _ in range(EMBEDDING_DIMENSIONS)) + "]"


def _app_connection(handle: DbHandle, tenant: uuid.UUID) -> psycopg.Connection:
    conn = psycopg.connect(handle.url_for("app_user", handle.passwords["app_user"]))
    conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))
    return conn


def _visible_chunks(handle: DbHandle, tenant: uuid.UUID, version: uuid.UUID) -> int:
    with _app_connection(handle, tenant) as conn:
        row = conn.execute(
            "SELECT count(*) FROM grounded.chunks WHERE version_id = %s", (version,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def _document_row(admin: psycopg.Connection, document: uuid.UUID) -> tuple[Any, Any]:
    row = admin.execute(
        "SELECT deleted_at, purge_after FROM grounded.documents WHERE id = %s", (document,)
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def _version_status(admin: psycopg.Connection, version: uuid.UUID) -> tuple[str, Any]:
    row = admin.execute(
        "SELECT status, retired_at FROM grounded.document_versions WHERE id = %s", (version,)
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def _ready_document(
    admin: psycopg.Connection, made: Made, seeded: Seeded, *, chunks: int = 2
) -> tuple[uuid.UUID, uuid.UUID]:
    """A live document whose current version is ready and has chunks."""
    document, version = make_version(
        admin,
        made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=POLICY_MD,
        status="ready",
    )
    admin.execute(
        "UPDATE grounded.documents SET current_version_id = %s WHERE id = %s", (version, document)
    )
    for position in range(chunks):
        admin.execute(
            "INSERT INTO grounded.chunks (tenant_id, version_id, position, body, embedding, "
            "embedding_model, embedding_model_version) VALUES (%s, %s, %s, 'text', %s::extensions.vector, %s, %s)",
            (seeded.a.tenant, version, position, _vector(), MODEL_NAME, MODEL_REVISION),
        )
    return document, version


@pytest.fixture
def delete_made(made: Made, admin: psycopg.Connection) -> Iterator[Made]:
    """The rows the tests create. The delete audit rows name the document, not the version, so they are removed here."""
    yield made
    for document in made.documents:
        admin.execute(
            "DELETE FROM grounded.audit_log WHERE action = 'document.delete' AND target_id = %s",
            (document,),
        )


def _admin_token(client: TestClient, accounts: Accounts) -> str:
    response = client.post(
        "/auth/login", json={"email": accounts.admin_email, "password": accounts.password}
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


def _employee_token(client: TestClient, accounts: Accounts) -> str:
    response = client.post(
        "/auth/login", json={"email": accounts.employee_email, "password": accounts.password}
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


def _delete(client: TestClient, token: str, document: uuid.UUID) -> Any:
    return client.delete(f"/documents/{document}", headers={"Authorization": f"Bearer {token}"})


# ---- the HTTP contract ----


def test_the_admin_deletes_and_retrieval_drops_the_chunks_on_commit(
    client: TestClient, accounts: Accounts, admin: psycopg.Connection, delete_made: Made
) -> None:
    seeded = accounts.seeded
    document, version = _ready_document(admin, delete_made, seeded, chunks=2)
    delete_made.documents.append(document)
    assert _visible_chunks(accounts.handle, seeded.a.tenant, version) == 2

    response = _delete(client, _admin_token(client, accounts), document)

    assert response.status_code == 204, response.text
    assert _visible_chunks(accounts.handle, seeded.a.tenant, version) == 0, (
        "retrieval must drop them at once"
    )
    deleted_at, purge_after = _document_row(admin, document)
    assert deleted_at is not None
    assert timedelta(days=TEMPORARY_RETENTION_DAYS - 1) < purge_after - deleted_at
    assert purge_after - deleted_at < timedelta(days=TEMPORARY_RETENTION_DAYS + 1)
    assert _version_status(admin, version)[0] == "retired"
    assert _version_status(admin, version)[1] is not None


def test_the_audit_row_holds_counts_and_no_title_or_filename(
    client: TestClient, accounts: Accounts, admin: psycopg.Connection, delete_made: Made
) -> None:
    seeded = accounts.seeded
    document, _version = _ready_document(admin, delete_made, seeded, chunks=1)
    delete_made.documents.append(document)
    title = admin.execute(
        "SELECT title FROM grounded.documents WHERE id = %s", (document,)
    ).fetchone()
    assert title is not None

    assert _delete(client, _admin_token(client, accounts), document).status_code == 204

    rows = admin.execute(
        "SELECT actor_user_id, target_type, target_id, detail FROM grounded.audit_log "
        "WHERE action = 'document.delete' AND target_id = %s",
        (document,),
    ).fetchall()
    assert len(rows) == 1
    actor, target_type, target_id, detail = rows[0]
    assert actor == seeded.a.admin
    assert target_type == "document"
    assert str(target_id) == str(document)
    assert detail == {"versions_retired": 1}
    assert title[0] not in json.dumps(detail)


def test_a_second_delete_and_an_unknown_document_get_the_same_404(
    client: TestClient, accounts: Accounts, admin: psycopg.Connection, delete_made: Made
) -> None:
    """After the first delete the document is absent to every read, so the second delete says what a read says.
    An unknown id gets the same answer, so a 404 does not reveal whether an id once existed (see the handler)."""
    seeded = accounts.seeded
    document, _version = _ready_document(admin, delete_made, seeded, chunks=0)
    delete_made.documents.append(document)
    token = _admin_token(client, accounts)

    first = _delete(client, token, document)
    second = _delete(client, token, document)
    unknown = _delete(client, token, uuid.uuid4())

    assert first.status_code == 204
    assert second.status_code == 404 and second.json() == NOT_FOUND
    assert unknown.status_code == 404 and unknown.json() == NOT_FOUND
    assert len(_audit_rows(admin, document)) == 1, "the repeat must write nothing"


def _audit_rows(admin: psycopg.Connection, document: uuid.UUID) -> list[tuple[Any, ...]]:
    return admin.execute(
        "SELECT id FROM grounded.audit_log WHERE action = 'document.delete' AND target_id = %s",
        (document,),
    ).fetchall()


def test_an_employee_is_refused_and_the_document_stays(
    client: TestClient, accounts: Accounts, admin: psycopg.Connection, delete_made: Made
) -> None:
    seeded = accounts.seeded
    document, _version = _ready_document(admin, delete_made, seeded, chunks=0)
    delete_made.documents.append(document)

    response = _delete(client, _employee_token(client, accounts), document)

    assert response.status_code == 403
    assert _document_row(admin, document)[0] is None


def test_another_tenants_admin_gets_the_404_and_the_document_stays(
    client: TestClient, accounts: Accounts, admin: psycopg.Connection, delete_made: Made
) -> None:
    seeded = accounts.seeded
    document, version = _ready_document(admin, delete_made, seeded, chunks=1)
    delete_made.documents.append(document)
    original = admin.execute(
        "SELECT password_hash FROM grounded.users WHERE id = %s", (seeded.b.admin,)
    ).fetchone()
    assert original is not None
    admin.execute(
        "UPDATE grounded.users SET password_hash = %s WHERE id = %s",
        (hash_password(accounts.password), seeded.b.admin),
    )
    try:
        b_email = admin.execute(
            "SELECT email FROM grounded.users WHERE id = %s", (seeded.b.admin,)
        ).fetchone()
        assert b_email is not None
        login = client.post(
            "/auth/login", json={"email": b_email[0], "password": accounts.password}
        )
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]

        response = _delete(client, token, document)
    finally:
        admin.execute(
            "UPDATE grounded.users SET password_hash = %s WHERE id = %s",
            (original[0], seeded.b.admin),
        )

    assert response.status_code == 404 and response.json() == NOT_FOUND
    assert _document_row(admin, document)[0] is None
    assert _version_status(admin, version)[0] == "ready"
    assert _audit_rows(admin, document) == []


def test_every_live_version_is_retired_and_the_retired_one_keeps_its_time(
    client: TestClient, accounts: Accounts, admin: psycopg.Connection, delete_made: Made
) -> None:
    seeded = accounts.seeded
    document, ready = make_version(
        admin,
        delete_made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="ready",
        version_no=2,
    )
    delete_made.documents.append(document)
    admin.execute(
        "UPDATE grounded.documents SET current_version_id = %s WHERE id = %s", (ready, document)
    )
    _, queued = make_version(
        admin,
        delete_made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="queued",
        version_no=3,
    )
    admin.execute(
        "UPDATE grounded.document_versions SET document_id = %s WHERE id = %s", (document, queued)
    )
    _, processing = make_version(
        admin,
        delete_made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="processing",
        version_no=4,
    )
    admin.execute(
        "UPDATE grounded.document_versions SET document_id = %s WHERE id = %s",
        (document, processing),
    )
    _, failed = make_version(
        admin,
        delete_made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="failed",
        version_no=5,
    )
    admin.execute(
        "UPDATE grounded.document_versions SET document_id = %s WHERE id = %s", (document, failed)
    )
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    _, old = make_version(
        admin,
        delete_made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=None,
        status="retired",
        version_no=6,
    )
    admin.execute(
        "UPDATE grounded.document_versions SET document_id = %s, retired_at = %s, purge_after = %s WHERE id = %s",
        (document, long_ago, long_ago + timedelta(days=30), old),
    )

    response = _delete(client, _admin_token(client, accounts), document)

    assert response.status_code == 204, response.text
    rows = admin.execute(
        "SELECT status FROM grounded.document_versions WHERE document_id = %s", (document,)
    ).fetchall()
    assert all(row[0] == "retired" for row in rows)
    assert _version_status(admin, old)[1] == long_ago, (
        "an already retired version keeps its retirement time"
    )
    assert _audit_rows(admin, document) != []
    detail = admin.execute(
        "SELECT detail FROM grounded.audit_log WHERE action = 'document.delete' AND target_id = %s",
        (document,),
    ).fetchone()
    assert detail is not None and detail[0] == {"versions_retired": 4}


# ---- the race with a worker ----


@asynccontextmanager
async def _app_sessions(handle: DbHandle) -> AsyncIterator[Any]:
    engine = create_database_engine(
        handle.url_for("app_user", handle.passwords["app_user"]).replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
    )
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


class DeleteWhileEmbedding(FakeEmbedder):
    """Commits the delete from the event loop, while the worker's thread is in the middle of embedding (ADR 0022)."""

    def __init__(self, loop: asyncio.AbstractEventLoop, schedule: Any) -> None:
        super().__init__()
        self._loop = loop
        self._schedule = schedule

    def embed_passages(self, texts: Any) -> Any:
        vectors = super().embed_passages(texts)
        asyncio.run_coroutine_threadsafe(self._schedule(), self._loop).result(timeout=30)
        return vectors


@pytest.mark.anyio
async def test_a_worker_finishing_a_deleted_version_loses_cleanly(
    accounts: Accounts, admin: psycopg.Connection, delete_made: Made
) -> None:
    """The version is claimed, then deleted while it is embedded. Its finish matches zero rows (the fencing token
    and the processing status), so it writes no chunks and no ready audit row, and reports LOST."""
    seeded = accounts.seeded
    document, version = make_version(
        admin,
        delete_made,
        seeded.a.tenant,
        seeded.a.admin,
        content_type=MARKDOWN,
        data=POLICY_MD,
        status="queued",
    )
    delete_made.documents.append(document)
    loop = asyncio.get_running_loop()
    deleted: dict[str, Any] = {}

    async with _app_sessions(accounts.handle) as factory:

        async def delete_now() -> None:
            context = TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
            async with begin_tenant_transaction(factory, context) as session:
                deleted["versions"] = await retire_document(
                    session,
                    tenant_id=seeded.a.tenant,
                    document_id=document,
                    actor_user_id=seeded.a.admin,
                    retention_days=TEMPORARY_RETENTION_DAYS,
                )

        job = IngestJob(
            tenant_id=seeded.a.tenant,
            document_id=document,
            version_id=version,
            attempt_token=uuid.uuid4(),
            lease_seconds=120,
            retention_days=30,
        )
        # The worker runs as worker_user, as in production. The delete runs as app_user, from the event loop.
        async with worker_sessions(accounts.handle) as worker_factory:
            outcome = await run_ingestion(
                session_factory=worker_factory,
                object_store=PostgresObjectStore(),
                embedder=DeleteWhileEmbedding(loop, delete_now),
                job=job,
            )

    assert deleted["versions"] == 1, "the delete must have happened during the embedding"
    assert outcome is Outcome.LOST
    assert version_row(admin, version)[0] == "retired"
    assert chunk_count(admin, version) == 0
    assert ready_audit_count(admin, version) == 0


# ---- the transition guard ----


def test_app_user_cannot_restore_a_deleted_document(
    client: TestClient, accounts: Accounts, admin: psycopg.Connection, delete_made: Made
) -> None:
    """The delete path sets deleted_at once. Clearing it is a restore, and restore is not supported (ADR 0025)."""
    seeded = accounts.seeded
    document, _version = _ready_document(admin, delete_made, seeded, chunks=0)
    delete_made.documents.append(document)
    assert _delete(client, _admin_token(client, accounts), document).status_code == 204

    with _app_connection(accounts.handle, seeded.a.tenant) as conn:
        with pytest.raises(
            errors.InsufficientPrivilege, match="may not restore a deleted document"
        ):
            conn.execute(
                "UPDATE grounded.documents SET deleted_at = NULL, purge_after = NULL WHERE id = %s",
                (document,),
            )
    assert _document_row(admin, document)[0] is not None

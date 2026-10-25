"""
Setup for the ingestion tests (ADR 0022): document versions with their bytes, made through the admin connection.

Setup rows are committed, because the worker role must see them. Each test removes what it made, in an order
that respects the foreign keys (chunks, then bytes, then versions, then documents).
"""

import secrets
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from grounded_answers.database import create_database_engine, create_session_factory
from grounded_answers.jobs.app import build_app
from grounded_answers.jobs.tasks import INGEST_DOCUMENT
from grounded_answers.models import EMBEDDING_DIMENSIONS

from .database import DbHandle


@dataclass
class Made:
    documents: list[uuid.UUID] = field(default_factory=list)
    versions: list[uuid.UUID] = field(default_factory=list)


class FakeEmbedder:
    """Deterministic vectors of the right size, for tests that are not about the model. Records its calls."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def query_embedding(self, text: str) -> list[float]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [float((len(text) + i) % 7) / 7.0 for i in range(EMBEDDING_DIMENSIONS)]


class ExplodingEmbedder:
    """Raises a runtime error whose message holds a marker. The marker must reach neither logs nor the reason."""

    def __init__(self, marker: str) -> None:
        self.marker = marker

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError(f"the model rejected {self.marker}")


@pytest.fixture
def admin(grounded_db: DbHandle) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(grounded_db.admin_test_url(), autocommit=True)
    yield conn
    conn.close()


@pytest.fixture
def made(admin: psycopg.Connection) -> Iterator[Made]:
    rows = Made()
    yield rows
    for document in rows.documents:
        admin.execute(
            "UPDATE grounded.documents SET current_version_id = NULL WHERE id = %s", (document,)
        )
    for version in rows.versions:
        admin.execute("DELETE FROM grounded.chunks WHERE version_id = %s", (version,))
        admin.execute("DELETE FROM grounded.audit_log WHERE target_id = %s", (version,))
        admin.execute("DELETE FROM grounded.document_blobs WHERE version_id = %s", (version,))
        admin.execute("DELETE FROM grounded.document_versions WHERE id = %s", (version,))
    for document in rows.documents:
        admin.execute("DELETE FROM grounded.documents WHERE id = %s", (document,))


def make_version(
    admin: psycopg.Connection,
    made: Made,
    tenant: uuid.UUID,
    creator: uuid.UUID,
    *,
    content_type: str,
    data: bytes | None,
    status: str = "queued",
    title: str = "Expense Policy",
    version_no: int = 1,
    document: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A document with one version in the given status, and its bytes when data is given. Returns (document, version).

    Pass an existing document to add a later version to it (a replacement)."""
    if document is None:
        document = uuid.uuid4()
        admin.execute(
            "INSERT INTO grounded.documents (id, tenant_id, title) VALUES (%s, %s, %s)",
            (document, tenant, title),
        )
        made.documents.append(document)
    version = uuid.uuid4()
    retired = status in ("retired", "purging")
    now = datetime.now(UTC)
    admin.execute(
        """
        INSERT INTO grounded.document_versions (
          id, tenant_id, document_id, version_no, original_filename, content_type,
          size_bytes, sha256, storage_key, status, created_by, retired_at, purge_after,
          processing_started_at, processing_token
        ) VALUES (%s, %s, %s, %s, 'upload.bin', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            version,
            tenant,
            document,
            version_no,
            content_type,
            len(data) if data else 1,
            secrets.token_bytes(32),
            f"tenant/{tenant}/{uuid.uuid4()}",
            status,
            creator,
            now if retired else None,
            now + timedelta(days=30) if retired else None,
            now if status == "processing" else None,
            uuid.uuid4() if status == "processing" else None,
        ),
    )
    made.versions.append(version)
    if data is not None:
        admin.execute(
            "INSERT INTO grounded.document_blobs (tenant_id, version_id, content, byte_size) "
            "VALUES (%s, %s, %s, %s)",
            (tenant, version, data, len(data)),
        )
    return document, version


def age_lease(admin: psycopg.Connection, version: uuid.UUID) -> None:
    """Make a processing version's lease look expired, as if its worker had stopped."""
    admin.execute(
        "UPDATE grounded.document_versions SET processing_started_at = now() - interval '2 hours' "
        "WHERE id = %s",
        (version,),
    )


def version_row(admin: psycopg.Connection, version: uuid.UUID) -> tuple[str, str | None]:
    row = admin.execute(
        "SELECT status, failure_reason FROM grounded.document_versions WHERE id = %s", (version,)
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def chunk_count(admin: psycopg.Connection, version: uuid.UUID) -> int:
    row = admin.execute(
        "SELECT count(*) FROM grounded.chunks WHERE version_id = %s", (version,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def ready_audit_count(admin: psycopg.Connection, version: uuid.UUID) -> int:
    row = admin.execute(
        "SELECT count(*) FROM grounded.audit_log WHERE target_id = %s AND action = 'document.ready'",
        (version,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def worker_url(handle: DbHandle) -> str:
    return handle.url_for("worker_user", handle.passwords["worker_user"]).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )


async def enqueue_ingest(
    handle: DbHandle, tenant: uuid.UUID, document: uuid.UUID, version: uuid.UUID
) -> None:
    """Queue one ingest job as app_user, the way the upload endpoint does (ADR 0015)."""
    app = build_app(handle.url_for("app_user", handle.passwords["app_user"]))
    async with app.open_async():
        await app.configure_task(name=INGEST_DOCUMENT).defer_async(
            tenant_id=str(tenant), document_id=str(document), version_id=str(version)
        )


@asynccontextmanager
async def worker_sessions(handle: DbHandle) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory for the worker role, with its own engine. The engine is disposed at the end."""
    engine = create_database_engine(worker_url(handle))
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()

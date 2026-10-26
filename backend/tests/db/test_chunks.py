"""
chunks (ADR 0021, migration 0008): who may read, write and delete a document's chunks.

The rules are enforced by grants and RLS, and each one is tested against the real database:
- app_user reads a chunk only when its version is 'ready', is the document's current version, and the
  document is not deleted (ARCHITECTURE 5.6).
- worker_user reads and deletes chunks of 'processing' or 'purging' versions, and inserts only into
  'processing' versions. A retried ingestion deletes its partial chunks first, so one set remains.
- Tenant isolation and fail-closed behaviour hold as on the other tenant tables.
- The vector dimension is fixed at 384; there is no vector index (ADR 0030).

Setup rows are committed (other roles must see them), so each test removes the rows it created.
"""

import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors

from .database import DbHandle
from .seed import Seeded

pytestmark = pytest.mark.db

DIMENSIONS = 384
MODEL = "intfloat/multilingual-e5-small"
MODEL_REVISION = "test-revision"


@dataclass
class Made:
    """Ids of the rows a test created through the admin connection, removed at the end."""

    documents: list[uuid.UUID] = field(default_factory=list)
    versions: list[uuid.UUID] = field(default_factory=list)


def _as(handle: DbHandle, role: str, tenant: uuid.UUID | None = None) -> psycopg.Connection:
    conn = psycopg.connect(handle.url_for(role, handle.passwords[role]))
    if tenant is not None:
        conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))
    return conn


def _vector(value: float = 0.1) -> str:
    return "[" + ",".join(str(value) for _ in range(DIMENSIONS)) + "]"


@pytest.fixture
def admin(grounded_db: DbHandle) -> Iterator[psycopg.Connection]:
    """Cluster admin, autocommit, for setup only: it bypasses RLS, as seed data does."""
    conn = psycopg.connect(grounded_db.admin_test_url(), autocommit=True)
    yield conn
    conn.close()


@pytest.fixture
def made(admin: psycopg.Connection) -> Iterator[Made]:
    """Remove what the test created: chunks first (RESTRICT), then the document's current pointer, then
    versions, then documents."""
    rows = Made()
    yield rows
    for document in rows.documents:
        admin.execute(
            "UPDATE grounded.documents SET current_version_id = NULL WHERE id = %s", (document,)
        )
    for version in reversed(rows.versions):
        admin.execute("DELETE FROM grounded.chunks WHERE version_id = %s", (version,))
        admin.execute("DELETE FROM grounded.document_versions WHERE id = %s", (version,))
    for document in reversed(rows.documents):
        admin.execute("DELETE FROM grounded.documents WHERE id = %s", (document,))


def _document(admin: psycopg.Connection, made: Made, tenant: uuid.UUID) -> uuid.UUID:
    document = uuid.uuid4()
    admin.execute(
        "INSERT INTO grounded.documents (id, tenant_id, title) VALUES (%s, %s, 'Chunk test')",
        (document, tenant),
    )
    made.documents.append(document)
    return document


def _version(
    admin: psycopg.Connection,
    made: Made,
    tenant: uuid.UUID,
    document: uuid.UUID,
    creator: uuid.UUID,
    status: str,
    *,
    version_no: int = 1,
) -> uuid.UUID:
    """A version in the given status, of an existing document. Committed. A processing version carries a lease."""
    version = uuid.uuid4()
    retired = status in ("retired", "purging")
    now = datetime.now(UTC)
    admin.execute(
        """
        INSERT INTO grounded.document_versions (
          id, tenant_id, document_id, version_no, original_filename, content_type,
          size_bytes, sha256, storage_key, status, created_by, retired_at, purge_after,
          processing_started_at, processing_token
        ) VALUES (%s, %s, %s, %s, 'c.txt', 'text/plain', 16, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            version,
            tenant,
            document,
            version_no,
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
    return version


def _point_current(admin: psycopg.Connection, document: uuid.UUID, version: uuid.UUID) -> None:
    admin.execute(
        "UPDATE grounded.documents SET current_version_id = %s WHERE id = %s", (version, document)
    )


def _ready_document(
    admin: psycopg.Connection, made: Made, tenant: uuid.UUID, creator: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """A document whose current version is ready. Returns (document, version)."""
    document = _document(admin, made, tenant)
    version = _version(admin, made, tenant, document, creator, "ready")
    _point_current(admin, document, version)
    return document, version


def _chunks(
    admin: psycopg.Connection, tenant: uuid.UUID, version: uuid.UUID, count: int = 2
) -> None:
    """Chunk rows written as the admin (test setup). Committed."""
    for position in range(count):
        admin.execute(
            """
            INSERT INTO grounded.chunks (
              tenant_id, version_id, position, section_number, heading, clause_labels, body,
              embedding, embedding_model, embedding_model_version
            ) VALUES (%s, %s, %s, '3.2', 'Probation', '{3.2.2}', 'Probation is 6 months.',
                      %s::extensions.vector, %s, %s)
            """,
            (tenant, version, position, _vector(), MODEL, MODEL_REVISION),
        )


def _count_visible(handle: DbHandle, tenant: uuid.UUID | None, version: uuid.UUID) -> int:
    with _as(handle, "app_user", tenant) as app:
        row = app.execute(
            "SELECT count(*) FROM grounded.chunks WHERE version_id = %s", (version,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_app_user_reads_a_chunk_of_a_ready_current_undeleted_document(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    _, version = _ready_document(admin, made, seeded.a.tenant, seeded.a.admin)
    _chunks(admin, seeded.a.tenant, version, count=2)
    assert _count_visible(grounded_db, seeded.a.tenant, version) == 2


@pytest.mark.parametrize("status", ["processing", "failed", "retired"])
def test_app_user_does_not_read_chunks_of_a_version_that_is_not_ready(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    status: str,
) -> None:
    document = _document(admin, made, seeded.a.tenant)
    version = _version(admin, made, seeded.a.tenant, document, seeded.a.admin, status)
    _point_current(admin, document, version)
    _chunks(admin, seeded.a.tenant, version, count=2)
    assert _count_visible(grounded_db, seeded.a.tenant, version) == 0


def test_app_user_does_not_read_chunks_of_a_superseded_ready_version(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document = _document(admin, made, seeded.a.tenant)
    old = _version(admin, made, seeded.a.tenant, document, seeded.a.admin, "ready", version_no=1)
    new = _version(admin, made, seeded.a.tenant, document, seeded.a.admin, "ready", version_no=2)
    _point_current(admin, document, new)
    _chunks(admin, seeded.a.tenant, old, count=2)
    _chunks(admin, seeded.a.tenant, new, count=2)
    assert _count_visible(grounded_db, seeded.a.tenant, old) == 0
    assert _count_visible(grounded_db, seeded.a.tenant, new) == 2


def test_app_user_does_not_read_chunks_of_a_deleted_document(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document, version = _ready_document(admin, made, seeded.a.tenant, seeded.a.admin)
    _chunks(admin, seeded.a.tenant, version, count=2)
    now = datetime.now(UTC)
    admin.execute(
        "UPDATE grounded.documents SET deleted_at = %s, purge_after = %s WHERE id = %s",
        (now, now + timedelta(days=30), document),
    )
    assert _count_visible(grounded_db, seeded.a.tenant, version) == 0


def test_app_user_of_another_tenant_reads_no_chunks(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    _, version = _ready_document(admin, made, seeded.a.tenant, seeded.a.admin)
    _chunks(admin, seeded.a.tenant, version, count=2)
    assert _count_visible(grounded_db, seeded.b.tenant, version) == 0
    assert _count_visible(grounded_db, seeded.a.tenant, version) == 2


def test_app_user_with_no_tenant_set_reads_no_chunks(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    _, version = _ready_document(admin, made, seeded.a.tenant, seeded.a.admin)
    _chunks(admin, seeded.a.tenant, version, count=2)
    assert _count_visible(grounded_db, None, version) == 0


def test_worker_inserts_chunks_only_into_a_processing_version(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document = _document(admin, made, seeded.a.tenant)
    processing = _version(admin, made, seeded.a.tenant, document, seeded.a.admin, "processing")
    with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
        worker.execute(
            "INSERT INTO grounded.chunks (tenant_id, version_id, position, body, embedding, "
            "embedding_model, embedding_model_version) "
            "VALUES (%s, %s, 0, 'text', %s::extensions.vector, %s, %s)",
            (seeded.a.tenant, processing, _vector(), MODEL, MODEL_REVISION),
        )

    ready = _version(admin, made, seeded.a.tenant, document, seeded.a.admin, "ready", version_no=2)
    with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
        with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
            worker.execute(
                "INSERT INTO grounded.chunks (tenant_id, version_id, position, body, embedding, "
                "embedding_model, embedding_model_version) "
                "VALUES (%s, %s, 0, 'text', %s::extensions.vector, %s, %s)",
                (seeded.a.tenant, ready, _vector(), MODEL, MODEL_REVISION),
            )


def test_worker_delete_removes_the_chunks_of_a_purging_version(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    document = _document(admin, made, seeded.a.tenant)
    purging = _version(admin, made, seeded.a.tenant, document, seeded.a.admin, "purging")
    _chunks(admin, seeded.a.tenant, purging, count=3)
    with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
        deleted = worker.execute(
            "DELETE FROM grounded.chunks WHERE version_id = %s", (purging,)
        ).rowcount
    # A DELETE that matches zero rows fails silently (ADR 0018 amendment). The count is checked.
    assert deleted == 3
    assert admin.execute(
        "SELECT count(*) FROM grounded.chunks WHERE version_id = %s", (purging,)
    ).fetchone() == (0,)


def test_a_crashed_then_retried_ingestion_leaves_exactly_one_set_of_chunks(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    """Ingestion commits in batches, so a crash can leave partial chunks. The retry deletes the version's
    chunks first. Without that delete, the retry hits the unique key on the chunk position."""
    document = _document(admin, made, seeded.a.tenant)
    version = _version(admin, made, seeded.a.tenant, document, seeded.a.admin, "processing")

    def insert_batch(conn: psycopg.Connection, positions: range) -> None:
        for position in positions:
            conn.execute(
                "INSERT INTO grounded.chunks (tenant_id, version_id, position, body, embedding, "
                "embedding_model, embedding_model_version) "
                "VALUES (%s, %s, %s, 'text', %s::extensions.vector, %s, %s)",
                (seeded.a.tenant, version, position, _vector(), MODEL, MODEL_REVISION),
            )

    # Attempt 1: batch one commits, then the worker crashes before batch two.
    with _as(grounded_db, "worker_user", seeded.a.tenant) as first:
        insert_batch(first, range(0, 2))
    # The crash is the end of attempt 1, after its first batch was committed.

    # Retry without the delete: the same positions collide with the committed partial set.
    with _as(grounded_db, "worker_user", seeded.a.tenant) as naive:
        with pytest.raises(errors.UniqueViolation):
            insert_batch(naive, range(0, 2))

    # Retry with the delete: one clean set, positions 0..4.
    with _as(grounded_db, "worker_user", seeded.a.tenant) as retry:
        retry.execute("DELETE FROM grounded.chunks WHERE version_id = %s", (version,))
        insert_batch(retry, range(0, 5))

    rows = admin.execute(
        "SELECT position FROM grounded.chunks WHERE version_id = %s ORDER BY position", (version,)
    ).fetchall()
    assert [row[0] for row in rows] == [0, 1, 2, 3, 4]


def test_a_chunk_with_the_wrong_vector_dimension_is_refused(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    """The column type refuses the wrong length first (pgvector raises DataException). The CHECK on
    vector_dims is a second guard that the migration also installs."""
    document = _document(admin, made, seeded.a.tenant)
    version = _version(admin, made, seeded.a.tenant, document, seeded.a.admin, "processing")
    for wrong in (DIMENSIONS - 1, DIMENSIONS + 1):
        vector = "[" + ",".join("0.1" for _ in range(wrong)) + "]"
        with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
            with pytest.raises(errors.DataException, match=f"expected {DIMENSIONS} dimensions"):
                worker.execute(
                    "INSERT INTO grounded.chunks (tenant_id, version_id, position, body, embedding, "
                    "embedding_model, embedding_model_version) "
                    "VALUES (%s, %s, %s, 'text', %s::extensions.vector, %s, %s)",
                    (seeded.a.tenant, version, 0, vector, MODEL, MODEL_REVISION),
                )


def test_there_is_no_index_on_the_embedding_and_the_keyword_index_remains(
    admin: psycopg.Connection,
) -> None:
    """Retrieval is an exact scan of the tenant's rows (ADR 0030, migration 0016). An approximate index that
    filters after its scan returned too few or wrong rows once a tenant had a few thousand chunks."""
    rows = admin.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'grounded' AND tablename = 'chunks'"
    ).fetchall()
    definitions: dict[str, str] = dict(rows)
    assert not [
        name for name, d in definitions.items() if "USING hnsw" in d or "USING ivfflat" in d
    ], "an approximate vector index exists on chunks"
    assert "USING gin" in definitions["chunks_tsv_gin"]
    assert "chunks_pkey" in definitions

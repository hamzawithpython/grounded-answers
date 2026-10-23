"""
document_blobs (ADR 0018, migration 0007): who may write, read and delete a version's bytes.

Least privilege is enforced by grants and RLS, and each rule is tested against the real database:
- app_user: INSERT only, and only for a 'queued' version of its own tenant.
- worker_user: SELECT only for 'queued' or 'processing' versions; DELETE only for 'purging' versions.
- Sizes and keys are checked by the database as well as by the upload code.

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

MAX_BYTES = 10 * 1024 * 1024


@dataclass
class Made:
    """Ids of the rows a test created through the admin connection, removed at the end."""

    documents: list[uuid.UUID] = field(default_factory=list)
    versions: list[uuid.UUID] = field(default_factory=list)


def _url(handle: DbHandle, role: str) -> str:
    return handle.url_for(role, handle.passwords[role])


def _as(handle: DbHandle, role: str, tenant: uuid.UUID | None = None) -> psycopg.Connection:
    conn = psycopg.connect(_url(handle, role))
    if tenant is not None:
        conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))
    return conn


@pytest.fixture
def admin(grounded_db: DbHandle) -> Iterator[psycopg.Connection]:
    """Cluster admin, autocommit, for setup only: it bypasses RLS, as seed data does."""
    conn = psycopg.connect(grounded_db.admin_test_url(), autocommit=True)
    yield conn
    conn.close()


@pytest.fixture
def made(admin: psycopg.Connection) -> Iterator[Made]:
    """Remove what the test created. Bytes first (RESTRICT), then versions, then documents."""
    rows = Made()
    yield rows
    for version in reversed(rows.versions):
        admin.execute("DELETE FROM grounded.document_blobs WHERE version_id = %s", (version,))
        admin.execute("DELETE FROM grounded.document_versions WHERE id = %s", (version,))
    for document in reversed(rows.documents):
        admin.execute("DELETE FROM grounded.documents WHERE id = %s", (document,))


def _version(
    admin: psycopg.Connection,
    made: Made,
    tenant: uuid.UUID,
    creator: uuid.UUID,
    status: str,
) -> uuid.UUID:
    """A version in the given status, in a new document of the tenant. Committed."""
    document = uuid.uuid4()
    admin.execute(
        "INSERT INTO grounded.documents (id, tenant_id, title) VALUES (%s, %s, 'Blob test')",
        (document, tenant),
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
        ) VALUES (%s, %s, %s, 1, 'b.txt', 'text/plain', 16, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            version,
            tenant,
            document,
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


def _blob(admin: psycopg.Connection, tenant: uuid.UUID, version: uuid.UUID) -> None:
    content = secrets.token_bytes(16)
    admin.execute(
        "INSERT INTO grounded.document_blobs (tenant_id, version_id, content, byte_size) "
        "VALUES (%s, %s, %s, %s)",
        (tenant, version, content, len(content)),
    )


def _blob_count(admin: psycopg.Connection, version: uuid.UUID) -> int:
    row = admin.execute(
        "SELECT count(*) FROM grounded.document_blobs WHERE version_id = %s", (version,)
    ).fetchone()
    assert row is not None
    return int(row[0])


# ---- app_user: the upload path -----------------------------------------------------------------


def test_app_user_can_store_bytes_for_its_own_queued_version(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    version = _version(admin, made, seeded.a.tenant, seeded.a.admin, "queued")
    with _as(grounded_db, "app_user", seeded.a.tenant) as app:
        app.execute(
            "INSERT INTO grounded.document_blobs (tenant_id, version_id, content, byte_size) "
            "VALUES (%s, %s, %s, 4)",
            (seeded.a.tenant, version, b"abcd"),
        )
        app.rollback()
    assert _blob_count(admin, version) == 0, "the rollback above must leave no bytes"


@pytest.mark.parametrize("status", ["ready", "processing", "failed", "retired", "purging"])
def test_app_user_cannot_attach_bytes_to_a_version_that_is_not_queued(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    status: str,
) -> None:
    version = _version(admin, made, seeded.a.tenant, seeded.a.admin, status)
    with _as(grounded_db, "app_user", seeded.a.tenant) as app:
        with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
            app.execute(
                "INSERT INTO grounded.document_blobs (tenant_id, version_id, content, byte_size) "
                "VALUES (%s, %s, %s, 4)",
                (seeded.a.tenant, version, b"abcd"),
            )
        app.rollback()


def test_app_user_cannot_attach_bytes_for_another_tenants_version(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    version_b = _version(admin, made, seeded.b.tenant, seeded.b.admin, "queued")
    with _as(grounded_db, "app_user", seeded.a.tenant) as app:
        with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
            app.execute(
                "INSERT INTO grounded.document_blobs (tenant_id, version_id, content, byte_size) "
                "VALUES (%s, %s, %s, 4)",
                (seeded.b.tenant, version_b, b"abcd"),
            )
        app.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT content FROM grounded.document_blobs",
        "SELECT byte_size FROM grounded.document_blobs",
        "UPDATE grounded.document_blobs SET byte_size = byte_size",
        "DELETE FROM grounded.document_blobs",
    ],
    ids=["select-content", "select-size", "update", "delete"],
)
def test_app_user_cannot_read_change_or_delete_bytes(grounded_db: DbHandle, statement: str) -> None:
    """The web app has no SELECT, UPDATE or DELETE on this table in v1 (ADR 0018, decision 3)."""
    with _as(grounded_db, "app_user") as app:
        with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
            app.execute(statement)
        app.rollback()


# ---- worker_user: ingestion reads and the purge deletes ----------------------------------------


@pytest.mark.parametrize("status", ["queued", "processing"])
def test_worker_can_read_bytes_for_an_ingestion_version(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    status: str,
) -> None:
    version = _version(admin, made, seeded.a.tenant, seeded.a.admin, status)
    _blob(admin, seeded.a.tenant, version)
    with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
        row = worker.execute(
            "SELECT count(*) FROM grounded.document_blobs WHERE version_id = %s", (version,)
        ).fetchone()
        assert row == (1,)
        worker.rollback()


def test_worker_can_read_bytes_of_a_purging_version_so_the_purge_can_delete_them(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    """DELETE needs the row visible through SELECT (ADR 0018, amendment). Read it, then delete it."""
    version = _version(admin, made, seeded.a.tenant, seeded.a.admin, "purging")
    _blob(admin, seeded.a.tenant, version)
    with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
        row = worker.execute(
            "SELECT count(*) FROM grounded.document_blobs WHERE version_id = %s", (version,)
        ).fetchone()
        assert row == (1,)
        worker.rollback()


@pytest.mark.parametrize("status", ["ready", "failed", "retired"])
def test_worker_cannot_read_bytes_outside_ingestion_and_purge(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    status: str,
) -> None:
    """RLS filters the rows silently: the read returns nothing, and raises nothing (ADR 0018)."""
    version = _version(admin, made, seeded.a.tenant, seeded.a.admin, status)
    _blob(admin, seeded.a.tenant, version)
    with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
        row = worker.execute(
            "SELECT count(*) FROM grounded.document_blobs WHERE version_id = %s", (version,)
        ).fetchone()
        assert row == (0,)
        worker.rollback()


def test_worker_deletes_bytes_of_a_purging_version(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    version = _version(admin, made, seeded.a.tenant, seeded.a.admin, "purging")
    _blob(admin, seeded.a.tenant, version)
    with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
        removed = worker.execute(
            "DELETE FROM grounded.document_blobs WHERE version_id = %s", (version,)
        ).rowcount
        assert removed == 1
        worker.rollback()


@pytest.mark.parametrize("status", ["queued", "processing", "ready", "retired"])
def test_worker_delete_leaves_bytes_of_a_version_that_is_not_purging(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    made: Made,
    status: str,
) -> None:
    """DELETE is filtered silently to 0 rows: the bytes stay, and no error is raised."""
    version = _version(admin, made, seeded.a.tenant, seeded.a.admin, status)
    _blob(admin, seeded.a.tenant, version)
    with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
        removed = worker.execute(
            "DELETE FROM grounded.document_blobs WHERE version_id = %s", (version,)
        ).rowcount
        assert removed == 0
        worker.rollback()
    assert _blob_count(admin, version) == 1


def test_worker_delete_of_missing_bytes_is_not_an_error(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    """Idempotent purge (ADR 0007): a row that is already gone counts as deleted."""
    version = _version(admin, made, seeded.a.tenant, seeded.a.admin, "purging")
    with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
        removed = worker.execute(
            "DELETE FROM grounded.document_blobs WHERE version_id = %s", (version,)
        ).rowcount
        assert removed == 0
        worker.rollback()


def test_worker_cannot_store_bytes(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    version = _version(admin, made, seeded.a.tenant, seeded.a.admin, "queued")
    with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
        with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
            worker.execute(
                "INSERT INTO grounded.document_blobs (tenant_id, version_id, content, byte_size) "
                "VALUES (%s, %s, %s, 4)",
                (seeded.a.tenant, version, b"abcd"),
            )
        worker.rollback()


def test_worker_in_another_tenant_sees_none_of_these_bytes(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    version = _version(admin, made, seeded.a.tenant, seeded.a.admin, "queued")
    _blob(admin, seeded.a.tenant, version)
    with _as(grounded_db, "worker_user", seeded.b.tenant) as worker:
        row = worker.execute(
            "SELECT count(*) FROM grounded.document_blobs WHERE version_id = %s", (version,)
        ).fetchone()
        assert row == (0,)
        worker.rollback()


def test_the_purge_order_removes_bytes_before_the_version_row(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, made: Made
) -> None:
    """The version row cannot be deleted while its bytes exist (RESTRICT). Bytes go first."""
    version = _version(admin, made, seeded.a.tenant, seeded.a.admin, "purging")
    _blob(admin, seeded.a.tenant, version)
    with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
        with pytest.raises(errors.ForeignKeyViolation):
            worker.execute("DELETE FROM grounded.document_versions WHERE id = %s", (version,))
        worker.rollback()
    with _as(grounded_db, "worker_user", seeded.a.tenant) as worker:
        blobs = worker.execute(
            "DELETE FROM grounded.document_blobs WHERE version_id = %s", (version,)
        ).rowcount
        versions = worker.execute(
            "DELETE FROM grounded.document_versions WHERE id = %s", (version,)
        ).rowcount
        assert (blobs, versions) == (1, 1)
        worker.rollback()


# ---- the database enforces the size and the key, not only the upload code -----------------------


@pytest.mark.parametrize(
    ("content_len", "byte_size"),
    [(0, 0), (4, 5), (MAX_BYTES + 1, MAX_BYTES + 1)],
    ids=["zero-bytes", "size-mismatch", "over-10-mib"],
)
def test_a_size_that_does_not_match_or_is_out_of_range_is_refused(
    admin: psycopg.Connection,
    seeded: Seeded,
    made: Made,
    content_len: int,
    byte_size: int,
) -> None:
    version = _version(admin, made, seeded.a.tenant, seeded.a.admin, "queued")
    with pytest.raises(errors.CheckViolation):
        admin.execute(
            "INSERT INTO grounded.document_blobs (tenant_id, version_id, content, byte_size) "
            "VALUES (%s, %s, %s, %s)",
            (seeded.a.tenant, version, b"x" * content_len, byte_size),
        )
    assert _blob_count(admin, version) == 0


def test_bytes_for_a_missing_version_are_refused(admin: psycopg.Connection, seeded: Seeded) -> None:
    with pytest.raises(errors.ForeignKeyViolation):
        admin.execute(
            "INSERT INTO grounded.document_blobs (tenant_id, version_id, content, byte_size) "
            "VALUES (%s, %s, %s, 4)",
            (seeded.a.tenant, uuid.uuid4(), b"abcd"),
        )

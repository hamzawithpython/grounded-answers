"""
worker_user's purge transitions (ADR 0007), enforced by policy.

Two RLS behaviours matter and are asserted separately:
- USING filters rows silently: an UPDATE or DELETE that the policy excludes affects 0 rows and
  raises nothing. So the test asserts the rowcount.
- WITH CHECK refuses a new row value with an error: InsufficientPrivilege, "row-level security".

Every write is rolled back, so the seed data does not change.
"""

import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors, sql

from .database import DbHandle
from .seed import Seeded

pytestmark = pytest.mark.db


def _worker(grounded_db: DbHandle) -> psycopg.Connection:
    return psycopg.connect(grounded_db.url_for("worker_user", grounded_db.passwords["worker_user"]))


def _admin(grounded_db: DbHandle) -> psycopg.Connection:
    return psycopg.connect(grounded_db.admin_test_url())


def _document(
    admin: psycopg.Connection,
    tenant: uuid.UUID,
    *,
    deleted: bool,
) -> uuid.UUID:
    doc = uuid.uuid4()
    now = datetime.now(UTC)
    admin.execute(
        "INSERT INTO grounded.documents (id, tenant_id, title, deleted_at, purge_after) "
        "VALUES (%s, %s, 'Worker', %s, %s)",
        (doc, tenant, now if deleted else None, now + timedelta(days=30) if deleted else None),
    )
    return doc


def _version(
    admin: psycopg.Connection,
    tenant: uuid.UUID,
    document: uuid.UUID,
    creator: uuid.UUID,
    *,
    status: str,
) -> uuid.UUID:
    """A version in the given status. Retired and purging rows carry retired_at and purge_after."""
    ver = uuid.uuid4()
    retired = status in ("retired", "purging")
    now = datetime.now(UTC)
    admin.execute(
        """
        INSERT INTO grounded.document_versions (
          id, tenant_id, document_id, version_no, original_filename, content_type,
          size_bytes, sha256, storage_key, status, created_by, retired_at, purge_after
        ) VALUES (%s, %s, %s, %s, 'w.txt', 'text/plain', 1, %s, %s, %s, %s, %s, %s)
        """,
        (
            ver,
            tenant,
            document,
            secrets.randbelow(10**6) + 2,
            secrets.token_bytes(32),
            f"tenant/{tenant}/{uuid.uuid4()}",
            status,
            creator,
            now if retired else None,
            now + timedelta(days=30) if retired else None,
        ),
    )
    return ver


@pytest.fixture
def worker(grounded_db: DbHandle) -> Iterator[psycopg.Connection]:
    conn = _worker(grounded_db)
    yield conn
    conn.rollback()
    conn.close()


def _set_tenant(conn: psycopg.Connection, tenant: uuid.UUID) -> None:
    conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))


def _version_status(grounded_db: DbHandle, ver: uuid.UUID) -> str | None:
    with _admin(grounded_db) as admin:
        row = admin.execute(
            "SELECT status FROM grounded.document_versions WHERE id = %s", (ver,)
        ).fetchone()
    return None if row is None else str(row[0])


# ---- document_versions: retired -> purging, and only that ----


def test_worker_moves_retired_to_purging(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    """Step 1 of the purge: a retired version in this tenant may become purging."""
    with _admin(grounded_db) as admin:
        doc = _document(admin, seeded.a.tenant, deleted=False)
        ver = _version(admin, seeded.a.tenant, doc, seeded.a.admin, status="retired")
        admin.commit()
    _set_tenant(worker, seeded.a.tenant)
    cur = worker.execute(
        "UPDATE grounded.document_versions SET status = 'purging' WHERE id = %s", (ver,)
    )
    assert cur.rowcount == 1


def test_worker_cannot_move_retired_to_ready(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    """WITH CHECK: the new status must be purging. Any other value is refused with an error."""
    with _admin(grounded_db) as admin:
        doc = _document(admin, seeded.a.tenant, deleted=False)
        ver = _version(admin, seeded.a.tenant, doc, seeded.a.admin, status="retired")
        admin.commit()
    _set_tenant(worker, seeded.a.tenant)
    with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
        worker.execute(
            "UPDATE grounded.document_versions SET status = 'ready' WHERE id = %s", (ver,)
        )


def test_worker_cannot_touch_a_ready_version(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    """USING: only retired rows are visible to the update, so a ready row is silently skipped."""
    with _admin(grounded_db) as admin:
        doc = _document(admin, seeded.a.tenant, deleted=False)
        ver = _version(admin, seeded.a.tenant, doc, seeded.a.admin, status="ready")
        admin.commit()
    _set_tenant(worker, seeded.a.tenant)
    cur = worker.execute(
        "UPDATE grounded.document_versions SET status = 'purging' WHERE id = %s", (ver,)
    )
    assert cur.rowcount == 0


def test_worker_deletes_only_purging_versions(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    """Step 6 of the purge: a retired (not yet purging) version is silently kept."""
    with _admin(grounded_db) as admin:
        doc = _document(admin, seeded.a.tenant, deleted=False)
        retired = _version(admin, seeded.a.tenant, doc, seeded.a.admin, status="retired")
        admin.commit()
    _set_tenant(worker, seeded.a.tenant)
    assert (
        worker.execute("DELETE FROM grounded.document_versions WHERE id = %s", (retired,)).rowcount
        == 0
    )


def test_worker_delete_succeeds_for_purging(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    """A purging version in the session tenant is deleted."""
    with _admin(grounded_db) as admin:
        doc = _document(admin, seeded.a.tenant, deleted=False)
        purging = _version(admin, seeded.a.tenant, doc, seeded.a.admin, status="purging")
        admin.commit()
    _set_tenant(worker, seeded.a.tenant)
    assert (
        worker.execute("DELETE FROM grounded.document_versions WHERE id = %s", (purging,)).rowcount
        == 1
    )


# ---- documents: clear current_version_id and delete, only once deleted ----


def test_worker_cannot_delete_a_live_document(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    with _admin(grounded_db) as admin:
        live = _document(admin, seeded.a.tenant, deleted=False)
        admin.commit()
    _set_tenant(worker, seeded.a.tenant)
    assert worker.execute("DELETE FROM grounded.documents WHERE id = %s", (live,)).rowcount == 0


def test_worker_cannot_delete_a_deleted_document_that_still_has_versions(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    """The NOT EXISTS in the DELETE policy: versions must be purged before the document."""
    with _admin(grounded_db) as admin:
        doc = _document(admin, seeded.a.tenant, deleted=True)
        _version(admin, seeded.a.tenant, doc, seeded.a.admin, status="retired")
        admin.commit()
    _set_tenant(worker, seeded.a.tenant)
    assert worker.execute("DELETE FROM grounded.documents WHERE id = %s", (doc,)).rowcount == 0


def test_worker_deletes_a_deleted_document_with_no_versions(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    with _admin(grounded_db) as admin:
        doc = _document(admin, seeded.a.tenant, deleted=True)
        admin.commit()
    _set_tenant(worker, seeded.a.tenant)
    assert worker.execute("DELETE FROM grounded.documents WHERE id = %s", (doc,)).rowcount == 1


def test_worker_clears_current_version_only_on_deleted_documents(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    """Step 2 of the purge: current_version_id may be set to NULL, and only on a deleted document."""
    with _admin(grounded_db) as admin:
        live = _document(admin, seeded.a.tenant, deleted=False)
        admin.commit()
    _set_tenant(worker, seeded.a.tenant)
    assert (
        worker.execute(
            "UPDATE grounded.documents SET current_version_id = NULL WHERE id = %s", (live,)
        ).rowcount
        == 0
    )


# ---- cross-tenant: none of the above reaches another tenant ----


def test_worker_cannot_move_another_tenants_retired_version(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    with _admin(grounded_db) as admin:
        doc = _document(admin, seeded.b.tenant, deleted=False)
        ver = _version(admin, seeded.b.tenant, doc, seeded.b.admin, status="retired")
        admin.commit()
    _set_tenant(worker, seeded.a.tenant)
    assert (
        worker.execute(
            "UPDATE grounded.document_versions SET status = 'purging' WHERE id = %s", (ver,)
        ).rowcount
        == 0
    )
    assert _version_status(grounded_db, ver) == "retired"


def test_worker_cannot_delete_another_tenants_purging_version(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    with _admin(grounded_db) as admin:
        doc = _document(admin, seeded.b.tenant, deleted=False)
        ver = _version(admin, seeded.b.tenant, doc, seeded.b.admin, status="purging")
        admin.commit()
    _set_tenant(worker, seeded.a.tenant)
    assert (
        worker.execute("DELETE FROM grounded.document_versions WHERE id = %s", (ver,)).rowcount == 0
    )
    assert _version_status(grounded_db, ver) == "purging"


def test_worker_audit_insert_refused_for_another_tenant(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    """audit_log INSERT: a row for tenant B with the session on A is refused by WITH CHECK."""
    _set_tenant(worker, seeded.a.tenant)
    with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
        worker.execute(
            "INSERT INTO grounded.audit_log (id, tenant_id, action) VALUES (%s, %s, 'injected')",
            (uuid.uuid4(), seeded.b.tenant),
        )


def test_worker_has_no_access_to_identity_or_tokens(
    grounded_db: DbHandle, seeded: Seeded, worker: psycopg.Connection
) -> None:
    """The purge job needs no users, memberships or tokens. Without a grant it cannot read them."""
    _set_tenant(worker, seeded.a.tenant)
    for table in ("users", "tenant_memberships", "refresh_tokens", "password_reset_tokens"):
        with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
            worker.execute(
                sql.SQL("SELECT id FROM {}").format(sql.Identifier("grounded", table))
            ).fetchall()
        worker.rollback()
        _set_tenant(worker, seeded.a.tenant)

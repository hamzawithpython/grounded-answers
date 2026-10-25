"""worker_user's lock-only policy on documents (ADR 0024, migration 0013).

The rule this relies on is in the PostgreSQL documentation (CREATE POLICY, the table of policy commands): a row locked
by SELECT ... FOR UPDATE is filtered by the USING expressions of SELECT and UPDATE policies, and no WITH CHECK
expression applies to the lock. The locking clause needs UPDATE privilege on at least one column (SELECT reference).

So the lock succeeds on a live document, and every UPDATE is still refused, because the WITH CHECK of the lock policy
is false and no other permissive WITH CHECK admits a live document's new row. The purge clear of a deleted document
(migration 0003) must still work.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors

from .database import DbHandle
from .seed import Seeded

pytestmark = pytest.mark.db


def _worker(handle: DbHandle, tenant: uuid.UUID) -> psycopg.Connection:
    conn = psycopg.connect(handle.url_for("worker_user", handle.passwords["worker_user"]))
    conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))
    return conn


@pytest.fixture
def live_document(admin: psycopg.Connection, seeded: Seeded) -> Iterator[uuid.UUID]:
    document = uuid.uuid4()
    admin.execute(
        "INSERT INTO grounded.documents (id, tenant_id, title) VALUES (%s, %s, 'Lock probe')",
        (document, seeded.a.tenant),
    )
    yield document
    admin.execute("DELETE FROM grounded.documents WHERE id = %s", (document,))


@pytest.fixture
def deleted_document(admin: psycopg.Connection, seeded: Seeded) -> Iterator[uuid.UUID]:
    document = uuid.uuid4()
    now = datetime.now(UTC)
    admin.execute(
        "INSERT INTO grounded.documents (id, tenant_id, title, deleted_at, purge_after) "
        "VALUES (%s, %s, 'Purge probe', %s, %s)",
        (document, seeded.a.tenant, now, now + timedelta(days=30)),
    )
    yield document
    admin.execute("DELETE FROM grounded.documents WHERE id = %s", (document,))


def test_worker_can_lock_a_live_document(
    grounded_db: DbHandle, seeded: Seeded, live_document: uuid.UUID
) -> None:
    with _worker(grounded_db, seeded.a.tenant) as worker:
        row = worker.execute(
            "SELECT id FROM grounded.documents WHERE id = %s FOR UPDATE", (live_document,)
        ).fetchone()
        worker.rollback()
    assert row is not None and row[0] == live_document


def test_worker_cannot_update_a_live_document_by_any_column(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, live_document: uuid.UUID
) -> None:
    """The lock policy's WITH CHECK is false, so no UPDATE of a live document is admitted. Each refusal is checked."""
    refused: list[tuple[str, tuple[object, ...]]] = [
        ("UPDATE grounded.documents SET title = 'changed' WHERE id = %s", (live_document,)),
        (
            "UPDATE grounded.documents SET current_version_id = %s WHERE id = %s",
            (uuid.uuid4(), live_document),
        ),
        (
            "UPDATE grounded.documents SET deleted_at = now(), purge_after = now() WHERE id = %s",
            (live_document,),
        ),
    ]
    for statement, params in refused:
        with _worker(grounded_db, seeded.a.tenant) as worker:
            with pytest.raises(errors.InsufficientPrivilege):
                worker.execute(statement, params)
    row = admin.execute(
        "SELECT title, deleted_at FROM grounded.documents WHERE id = %s", (live_document,)
    ).fetchone()
    assert row == ("Lock probe", None), "a refused update must change nothing"


def test_the_purge_clear_on_a_deleted_document_still_works(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, deleted_document: uuid.UUID
) -> None:
    """Step 2 of the purge (migration 0003) clears current_version_id on a deleted document. Lock-only access must not
    take that away: the purge policy is a separate permissive UPDATE policy."""
    with _worker(grounded_db, seeded.a.tenant) as worker:
        cursor = worker.execute(
            "UPDATE grounded.documents SET current_version_id = NULL WHERE id = %s",
            (deleted_document,),
        )
        assert cursor.rowcount == 1
        worker.commit()


def test_a_lock_on_a_deleted_document_is_not_a_write(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection, deleted_document: uuid.UUID
) -> None:
    """Recorded as observed, not assumed. The purge policy's USING admits deleted documents, and permissive policies
    are OR-ed, so the lock may succeed there. Either way the finish must not depend on it: the version's status and
    fencing token decide, and those are checked separately (test_document_delete, test_swap)."""
    with _worker(grounded_db, seeded.a.tenant) as worker:
        row = worker.execute(
            "SELECT id FROM grounded.documents WHERE id = %s FOR UPDATE", (deleted_document,)
        ).fetchone()
        worker.rollback()
    assert row == (deleted_document,), (
        "observed: the lock succeeds on a deleted document, because the purge policy admits deleted rows. "
        "The finish must therefore rely on the version's status and fencing token, not on the lock"
    )

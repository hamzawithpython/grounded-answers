"""The transition guard per role, as a full matrix (ADR 0026, migration 0010).

For each guarded role and each pair of statuses, a version is set to the start status by the cluster admin, and the
role then moves it to the target status in its own transaction. Every pair is listed as allowed or forbidden below.

A forbidden move is refused in one of two ways, and the test accepts either:
- the trigger raises insufficient_privilege ("<role> may not move a version ..."), or
- RLS refuses the new row, or RLS hides the row, so nothing changes.
In every refused case the stored status must still be the start status.

app_user's policy checks only the tenant, so for app_user the trigger is the only check. Every forbidden app_user
move reaches it. worker_user's policies admit only some moves, so some of its forbidden moves are refused by RLS.

Every row is removed afterwards.
"""

import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors

from .database import DbHandle
from .seed import Seeded

pytestmark = pytest.mark.db

STATUSES = ("queued", "processing", "ready", "failed", "retired", "purging")

# (from, to) pairs each role may make. A same-status write is always allowed by the trigger; whether RLS admits
# it depends on the role's policies, so worker_user lists only the same-status move its policies admit.
WORKER_ALLOWED = {
    ("queued", "processing"),
    ("processing", "ready"),
    ("processing", "failed"),
    ("retired", "purging"),
    ("processing", "processing"),
}
APP_ALLOWED = {
    ("queued", "retired"),
    ("processing", "retired"),
    ("ready", "retired"),
    ("failed", "retired"),
} | {(status, status) for status in STATUSES}

ROLE_ALLOWED = {"worker_user": WORKER_ALLOWED, "app_user": APP_ALLOWED}


def _admin(grounded_db: DbHandle) -> psycopg.Connection:
    return psycopg.connect(grounded_db.admin_test_url(), autocommit=True)


@pytest.fixture
def admin(grounded_db: DbHandle) -> Iterator[psycopg.Connection]:
    conn = _admin(grounded_db)
    yield conn
    conn.close()


def _make_version(
    admin: psycopg.Connection, seeded: Seeded, status: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """A live document and one version in the given status, written by the admin. Returns (document, version)."""
    tenant = seeded.a.tenant
    document = uuid.uuid4()
    version = uuid.uuid4()
    now = datetime.now(UTC)
    retired = status in ("retired", "purging")
    processing = status == "processing"
    admin.execute(
        "INSERT INTO grounded.documents (id, tenant_id, title) VALUES (%s, %s, 'Matrix')",
        (document, tenant),
    )
    admin.execute(
        """
        INSERT INTO grounded.document_versions (
          id, tenant_id, document_id, version_no, original_filename, content_type,
          size_bytes, sha256, storage_key, status, created_by, retired_at, purge_after,
          processing_started_at, processing_token
        ) VALUES (%s, %s, %s, 1, 'm.txt', 'text/plain', 1, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            version,
            tenant,
            document,
            secrets.token_bytes(32),
            f"tenant/{tenant}/{uuid.uuid4()}",
            status,
            seeded.a.admin,
            now if retired else None,
            now + timedelta(days=30) if retired else None,
            now if processing else None,
            uuid.uuid4() if processing else None,
        ),
    )
    return document, version


def _remove(admin: psycopg.Connection, document: uuid.UUID) -> None:
    admin.execute("DELETE FROM grounded.document_versions WHERE document_id = %s", (document,))
    admin.execute("DELETE FROM grounded.documents WHERE id = %s", (document,))


def _stored_status(admin: psycopg.Connection, version: uuid.UUID) -> str:
    row = admin.execute(
        "SELECT status FROM grounded.document_versions WHERE id = %s", (version,)
    ).fetchone()
    assert row is not None
    return str(row[0])


def _move(
    grounded_db: DbHandle, role: str, tenant: uuid.UUID, version: uuid.UUID, target: str
) -> str:
    """Try the move as role. Returns "moved", "hidden" (RLS matched no row) or "refused" (an error)."""
    now = datetime.now(UTC)
    # Each role sets only the columns its grants allow. worker_user has no grant on retired_at or purge_after.
    if role == "worker_user":
        sql = "UPDATE grounded.document_versions SET status = %s WHERE id = %s"
        params: tuple[object, ...] = (target, version)
        if target == "processing":
            sql = (
                "UPDATE grounded.document_versions SET status = %s, processing_started_at = %s, "
                "processing_token = %s WHERE id = %s"
            )
            params = (target, now, uuid.uuid4(), version)
        password = grounded_db.passwords["worker_user"]
    else:
        sql = "UPDATE grounded.document_versions SET status = %s WHERE id = %s"
        params = (target, version)
        if target in ("retired", "purging"):
            sql = (
                "UPDATE grounded.document_versions SET status = %s, retired_at = %s, purge_after = %s "
                "WHERE id = %s"
            )
            params = (target, now, now + timedelta(days=30), version)
        password = grounded_db.passwords["app_user"]
    url = grounded_db.url_for(role, password)
    try:
        with psycopg.connect(url) as conn:
            conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))
            moved = conn.execute(sql, params).rowcount
    except errors.InsufficientPrivilege as exc:
        message = str(exc)
        assert "may not move a version" in message or "row-level security" in message, message
        return "refused"
    return "moved" if moved == 1 else "hidden"


@pytest.mark.parametrize("role", ["worker_user", "app_user"])
@pytest.mark.parametrize("start", STATUSES)
@pytest.mark.parametrize("target", STATUSES)
def test_the_guard_admits_exactly_the_listed_moves(
    grounded_db: DbHandle,
    seeded: Seeded,
    admin: psycopg.Connection,
    role: str,
    start: str,
    target: str,
) -> None:
    document, version = _make_version(admin, seeded, start)
    try:
        outcome = _move(grounded_db, role, seeded.a.tenant, version, target)
        stored = _stored_status(admin, version)
        if (start, target) in ROLE_ALLOWED[role]:
            assert outcome == "moved", f"{role} should move {start} -> {target}, got {outcome}"
            assert stored == target
        else:
            assert outcome in ("refused", "hidden"), f"{role} must not move {start} -> {target}"
            assert stored == start, f"a refused move changed the status: {start} -> {stored}"
    finally:
        _remove(admin, document)


def test_a_refused_move_names_the_role_and_the_statuses(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection
) -> None:
    """The trigger's message names the role and both statuses, and it carries no document data."""
    document, version = _make_version(admin, seeded, "ready")
    url = grounded_db.url_for("app_user", grounded_db.passwords["app_user"])
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "SELECT set_config('app.current_tenant', %s, true)", (str(seeded.a.tenant),)
            )
            with pytest.raises(
                errors.InsufficientPrivilege,
                match="app_user may not move a version from ready to queued",
            ):
                conn.execute(
                    "UPDATE grounded.document_versions SET status = 'queued' WHERE id = %s",
                    (version,),
                )
    finally:
        _remove(admin, document)


def test_app_user_cannot_point_a_document_at_a_version(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection
) -> None:
    """app_user lost UPDATE on documents.current_version_id (ADR 0026). Only the worker sets it, and only when NULL."""
    document, version = _make_version(admin, seeded, "ready")
    url = grounded_db.url_for("app_user", grounded_db.passwords["app_user"])
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "SELECT set_config('app.current_tenant', %s, true)", (str(seeded.a.tenant),)
            )
            with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
                conn.execute(
                    "UPDATE grounded.documents SET current_version_id = %s WHERE id = %s",
                    (version, document),
                )
    finally:
        _remove(admin, document)


def test_app_user_keeps_the_document_columns_it_needs(
    grounded_db: DbHandle, seeded: Seeded, admin: psycopg.Connection
) -> None:
    """The revoke is narrow: app_user can still update the title (rolled back, so nothing changes)."""
    document, _version = _make_version(admin, seeded, "ready")
    url = grounded_db.url_for("app_user", grounded_db.passwords["app_user"])
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "SELECT set_config('app.current_tenant', %s, true)", (str(seeded.a.tenant),)
            )
            cursor = conn.execute(
                "UPDATE grounded.documents SET title = 'Renamed' WHERE id = %s", (document,)
            )
            assert cursor.rowcount == 1
            conn.rollback()
    finally:
        _remove(admin, document)


def test_an_unguarded_role_is_not_refused_by_the_trigger(
    seeded: Seeded, admin: psycopg.Connection
) -> None:
    """The cluster admin (the migration role in the tests) is not in the trigger's list. It can set any status,
    which the fixtures need. The trigger returns without a check for it."""
    document, version = _make_version(admin, seeded, "queued")
    try:
        admin.execute(
            "UPDATE grounded.document_versions SET status = 'ready' WHERE id = %s", (version,)
        )
        assert _stored_status(admin, version) == "ready"
    finally:
        _remove(admin, document)

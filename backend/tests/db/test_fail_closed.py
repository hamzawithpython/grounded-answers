"""
Fail-closed behaviour before any RLS policy exists (ADR 0004, ADR 0005, ADR 0006).

RLS is ENABLED and FORCED on every table, and no policy exists yet (Step 5 adds them).
With no policy, the answer is zero rows and no writes, for every role, even when the data
and the tenant context match. These tests prove that, so a later accidental permissive
policy is caught by a failing test, not by a data leak.

Seeding uses the cluster admin, because only a superuser bypasses RLS. The app role and
migration_role are both subject to RLS and cannot seed (see test_migration_role below).
"""

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import errors, sql

from .database import DbHandle

pytestmark = pytest.mark.db

TABLES = (
    "tenants",
    "users",
    "tenant_memberships",
    "documents",
    "document_versions",
    "audit_log",
    "refresh_tokens",
    "password_reset_tokens",
)


def _select_ids(table: str) -> sql.Composed:
    """SELECT id FROM grounded.<table>. Table names come from TABLES, never from input."""
    return sql.SQL("SELECT id FROM {}").format(sql.Identifier("grounded", table))


@pytest.fixture(scope="module")
def seeded(grounded_db: DbHandle) -> Iterator[uuid.UUID]:
    """One complete tenant with a row in every table. Returns the tenant id."""
    tenant = uuid.uuid4()
    user = uuid.uuid4()
    membership = uuid.uuid4()
    document = uuid.uuid4()
    version = uuid.uuid4()
    with psycopg.connect(grounded_db.admin_test_url(), autocommit=False) as admin:
        admin.execute(
            "INSERT INTO grounded.tenants (id, name, slug) VALUES (%s, 'Seed Co', %s)",
            (tenant, f"seed-{tenant.hex[:12]}"),
        )
        admin.execute(
            "INSERT INTO grounded.users (id, email, display_name) VALUES (%s, %s, 'Seed')",
            (user, f"seed-{user.hex[:12]}@example.com"),
        )
        admin.execute(
            "INSERT INTO grounded.tenant_memberships (id, tenant_id, user_id, role) "
            "VALUES (%s, %s, %s, 'tenant_admin')",
            (membership, tenant, user),
        )
        admin.execute(
            "INSERT INTO grounded.documents (id, tenant_id, title) VALUES (%s, %s, 'Seed')",
            (document, tenant),
        )
        admin.execute(
            """
            INSERT INTO grounded.document_versions (
              id, tenant_id, document_id, version_no, original_filename, content_type,
              size_bytes, sha256, storage_key, status, created_by
            ) VALUES (
              %s, %s, %s, 1, 'seed.txt', 'text/plain',
              10, %s, %s, 'queued', %s
            )
            """,
            (
                version,
                tenant,
                document,
                b"\x00" * 32,
                f"tenant/{tenant}/{uuid.uuid4()}",
                user,
            ),
        )
        admin.execute(
            "INSERT INTO grounded.audit_log (id, tenant_id, action) VALUES (%s, %s, 'seed')",
            (uuid.uuid4(), tenant),
        )
        admin.execute(
            "INSERT INTO grounded.refresh_tokens (id, tenant_id, user_id, family_id, "
            "token_hash, issued_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, now(), now() + interval '14 days')",
            (uuid.uuid4(), tenant, user, uuid.uuid4(), b"\x01" * 32),
        )
        admin.execute(
            "INSERT INTO grounded.password_reset_tokens (id, tenant_id, user_id, created_by, "
            "token_hash, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, now() + interval '1 day')",
            (uuid.uuid4(), tenant, user, user, b"\x02" * 32),
        )
        admin.commit()
        counts: dict[str, int] = {}
        for t in TABLES:
            row = admin.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("grounded", t))
            ).fetchone()
            assert row is not None
            counts[t] = row[0]
    assert all(count >= 1 for count in counts.values()), f"seed incomplete: {counts}"
    yield tenant


def _app_connection(grounded_db: DbHandle) -> psycopg.Connection:
    return psycopg.connect(grounded_db.url_for("app_user", grounded_db.passwords["app_user"]))


@pytest.mark.parametrize("table", TABLES)
def test_app_user_sees_zero_rows_without_tenant_context(
    grounded_db: DbHandle, seeded: uuid.UUID, table: str
) -> None:
    """No tenant set: fail closed, zero rows, even though the table is not empty."""
    with _app_connection(grounded_db) as conn, conn.transaction():
        rows = conn.execute(_select_ids(table)).fetchall()
    assert rows == [], f"{table}: app_user saw rows with no tenant context"


@pytest.mark.parametrize("table", TABLES)
def test_app_user_sees_zero_rows_even_with_the_matching_tenant(
    grounded_db: DbHandle, seeded: uuid.UUID, table: str
) -> None:
    """
    The seeded tenant is set as the context, and the data is there, but no policy exists.
    A policy added later must be the thing that makes rows visible, deliberately.
    """
    with _app_connection(grounded_db) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(seeded),))
        rows = conn.execute(_select_ids(table)).fetchall()
    assert rows == [], f"{table}: app_user saw rows without any policy"


def test_app_user_insert_is_rejected_by_rls(grounded_db: DbHandle, seeded: uuid.UUID) -> None:
    """INSERT is granted on documents, but with RLS on and no policy the row is refused."""
    with _app_connection(grounded_db) as conn:
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(seeded),))
        with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO grounded.documents (id, tenant_id, title) "
                    "VALUES (%s, %s, 'intruder')",
                    (uuid.uuid4(), seeded),
                )


def test_app_user_insert_without_grant_is_rejected(
    grounded_db: DbHandle, seeded: uuid.UUID
) -> None:
    """No INSERT grant on tenants at all: refused before RLS is even consulted."""
    with _app_connection(grounded_db) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO grounded.tenants (id, name, slug) VALUES (%s, 'x', 'intruder')",
                    (uuid.uuid4(),),
                )


def test_migration_role_cannot_write_without_a_policy(
    grounded_db: DbHandle, seeded: uuid.UUID
) -> None:
    """
    Finding (ADR 0004): migration_role owns the tables, but FORCE RLS applies to owners too.
    With no policy, even the owner cannot insert. Operator writes need the explicit
    migration_role policy, which is added in Step 5.
    """
    url = grounded_db.url_for("migration_role", grounded_db.passwords["migration_role"])
    with psycopg.connect(url) as conn:
        with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO grounded.tenants (id, name, slug) VALUES (%s, 'x', 'op-blocked')",
                    (uuid.uuid4(),),
                )

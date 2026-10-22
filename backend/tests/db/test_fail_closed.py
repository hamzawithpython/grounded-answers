"""
Read isolation as app_user (ADR 0004, ADR 0005, ADR 0006).

- No tenant context: zero rows on every table (fail closed).
- Tenant A's context: exactly A's rows, computed by the admin, and none of B's.
- password_hash is never readable by app_user.

Expected sets come from the cluster admin, which bypasses RLS, so the test compares the
policy's answer against the ground truth rather than against a hard-coded number.
"""

import uuid

import psycopg
import pytest
from psycopg import errors, sql

from .database import DbHandle
from .seed import Seeded

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
    "tenant_email_domains",
)


def _app(grounded_db: DbHandle) -> psycopg.Connection:
    return psycopg.connect(grounded_db.url_for("app_user", grounded_db.passwords["app_user"]))


def _ids_as_app(grounded_db: DbHandle, tenant: uuid.UUID | None, table: str) -> set[uuid.UUID]:
    """Ids app_user can see in table, with the given tenant context (None = no context)."""
    with _app(grounded_db) as conn, conn.transaction():
        if tenant is not None:
            conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))
        rows = conn.execute(
            sql.SQL("SELECT id FROM {}").format(sql.Identifier("grounded", table))
        ).fetchall()
    return {row[0] for row in rows}


def _expected_ids(grounded_db: DbHandle, tenant: uuid.UUID, table: str) -> set[uuid.UUID]:
    """Ground truth from the admin (RLS bypass): the rows that belong to tenant."""
    with psycopg.connect(grounded_db.admin_test_url()) as admin:
        if table == "tenants":
            query: sql.SQL | sql.Composed = sql.SQL("SELECT id FROM grounded.tenants WHERE id = %s")
        elif table == "users":
            query = sql.SQL(
                "SELECT u.id FROM grounded.users AS u WHERE EXISTS ("
                "SELECT 1 FROM grounded.tenant_memberships AS m "
                "WHERE m.user_id = u.id AND m.tenant_id = %s)"
            )
        else:
            query = sql.SQL("SELECT id FROM {} WHERE tenant_id = %s").format(
                sql.Identifier("grounded", table)
            )
        return {row[0] for row in admin.execute(query, (tenant,)).fetchall()}


@pytest.mark.parametrize("table", TABLES)
def test_no_tenant_context_sees_nothing(grounded_db: DbHandle, seeded: Seeded, table: str) -> None:
    """With no tenant set, every table returns zero rows even though it is not empty."""
    assert _ids_as_app(grounded_db, None, table) == set(), f"{table}: rows without a context"


@pytest.mark.parametrize("table", TABLES)
def test_tenant_context_sees_exactly_its_own_rows(
    grounded_db: DbHandle, seeded: Seeded, table: str
) -> None:
    """Tenant A sees A's rows: the set equals the admin's ground truth, and nothing of B's."""
    seen = _ids_as_app(grounded_db, seeded.a.tenant, table)
    expected = _expected_ids(grounded_db, seeded.a.tenant, table)
    assert expected, f"{table}: seed produced no rows for tenant A (test is vacuous)"
    assert seen == expected, f"{table}: app_user saw {seen ^ expected} wrongly"
    other = _expected_ids(grounded_db, seeded.b.tenant, table)
    assert not (seen & other), f"{table}: tenant B rows leaked into tenant A's view"


def test_app_user_cannot_read_password_hash(grounded_db: DbHandle, seeded: Seeded) -> None:
    """Column grants: the hash is write-only for app_user (ADR 0005)."""
    with _app(grounded_db) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(seeded.a.tenant),))
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT password_hash FROM grounded.users").fetchall()

"""
INSERT isolation as app_user, on every table (ADR 0004, ADR 0006, ADR 0007).

Each cross-tenant INSERT is refused, and the test says which layer refused it:
- "row-level security" = the grant exists, the policy's WITH CHECK refused the row.
- "permission denied"  = there is no INSERT grant at all (defence in depth: two gates).

Nothing is committed. Every connection is closed without commit, so the seed data is never
changed by these tests.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import errors, sql

from .database import DbHandle
from .seed import Seeded, TenantSeed

pytestmark = pytest.mark.db

# Tables app_user has an INSERT grant on. Every one of them has an INSERT policy with a tenant
# WITH CHECK, so a cross-tenant row is refused by RLS.
RLS_INSERT_TABLES = frozenset(
    {"documents", "document_versions", "audit_log", "refresh_tokens", "password_reset_tokens"}
)
# Tables with no INSERT grant for app_user. Refused by the grant, before any policy is checked.
NO_GRANT_TABLES = frozenset({"tenants", "users", "tenant_memberships", "tenant_email_domains"})


def _row(table: str, target: TenantSeed) -> dict[str, Any]:
    """A valid row for table, with tenant_id (where it exists) set to target's tenant."""
    now = datetime.now(UTC)
    new_id = uuid.uuid4()
    if table == "documents":
        return {"id": new_id, "tenant_id": target.tenant, "title": "Injected"}
    if table == "document_versions":
        return {
            "id": new_id,
            "tenant_id": target.tenant,
            "document_id": target.document,
            "version_no": 2,
            "original_filename": "injected.txt",
            "content_type": "text/plain",
            "size_bytes": 1,
            "sha256": secrets.token_bytes(32),
            "storage_key": f"tenant/{target.tenant}/{uuid.uuid4()}",
            "status": "queued",
            "created_by": target.admin,
        }
    if table == "audit_log":
        return {"id": new_id, "tenant_id": target.tenant, "actor_user_id": None, "action": "x"}
    if table == "refresh_tokens":
        return {
            "id": new_id,
            "tenant_id": target.tenant,
            "user_id": target.employee,
            "family_id": uuid.uuid4(),
            "token_hash": secrets.token_bytes(32),
            "issued_at": now,
            "expires_at": now + timedelta(days=14),
        }
    if table == "password_reset_tokens":
        return {
            "id": new_id,
            "tenant_id": target.tenant,
            "user_id": target.employee,
            "created_by": target.admin,
            "token_hash": secrets.token_bytes(32),
            "expires_at": now + timedelta(days=1),
        }
    if table == "tenant_email_domains":
        return {"id": new_id, "tenant_id": target.tenant, "domain": f"x{new_id.hex[:8]}.test"}
    if table == "tenant_memberships":
        return {
            "id": new_id,
            "tenant_id": target.tenant,
            "user_id": uuid.uuid4(),
            "role": "employee",
        }
    if table == "users":
        return {"id": new_id, "email": f"inj.{new_id.hex[:8]}@example.test", "display_name": "x"}
    if table == "tenants":
        return {"id": new_id, "name": "Injected", "slug": f"inj-{new_id.hex[:8]}"}
    raise AssertionError(f"no payload defined for {table}")


def _insert(conn: psycopg.Connection, table: str, row: dict[str, Any]) -> None:
    cols = list(row)
    stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier("grounded", table),
        sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        sql.SQL(", ").join(sql.Placeholder() for _ in cols),
    )
    conn.execute(stmt, [row[c] for c in cols])


def _app(grounded_db: DbHandle) -> psycopg.Connection:
    return psycopg.connect(grounded_db.url_for("app_user", grounded_db.passwords["app_user"]))


def _set_tenant(conn: psycopg.Connection, tenant: uuid.UUID) -> None:
    conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))


@pytest.mark.parametrize("table", sorted(RLS_INSERT_TABLES))
def test_cross_tenant_insert_refused_by_rls(
    grounded_db: DbHandle, seeded: Seeded, table: str
) -> None:
    """Context A, row for B: the grant exists, so RLS's WITH CHECK is what refuses the row."""
    with _app(grounded_db) as conn:
        _set_tenant(conn, seeded.a.tenant)
        with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
            _insert(conn, table, _row(table, seeded.b))


@pytest.mark.parametrize("table", sorted(NO_GRANT_TABLES))
def test_cross_tenant_insert_refused_by_grant(
    grounded_db: DbHandle, seeded: Seeded, table: str
) -> None:
    """No INSERT grant for app_user on this table: refused before any policy is consulted."""
    with _app(grounded_db) as conn:
        _set_tenant(conn, seeded.a.tenant)
        with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
            _insert(conn, table, _row(table, seeded.b))


def test_cross_tenant_audit_log_with_null_actor_refused(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    """The NULL-actor case is the one a naive policy misses: the tenant check must still hold."""
    with _app(grounded_db) as conn:
        _set_tenant(conn, seeded.a.tenant)
        row = _row("audit_log", seeded.b)
        assert row["actor_user_id"] is None
        with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
            _insert(conn, "audit_log", row)


@pytest.mark.parametrize("table", sorted(RLS_INSERT_TABLES))
def test_own_tenant_insert_succeeds_where_granted(
    grounded_db: DbHandle, seeded: Seeded, table: str
) -> None:
    """The same payload for the session's own tenant is accepted (rolled back afterwards)."""
    with _app(grounded_db) as conn:
        _set_tenant(conn, seeded.a.tenant)
        _insert(conn, table, _row(table, seeded.a))
        conn.rollback()


@pytest.mark.parametrize("table", sorted(NO_GRANT_TABLES))
def test_own_tenant_insert_still_refused_where_no_grant(
    grounded_db: DbHandle, seeded: Seeded, table: str
) -> None:
    """Being in the tenant is not enough: app_user has no INSERT grant on these tables."""
    with _app(grounded_db) as conn:
        _set_tenant(conn, seeded.a.tenant)
        with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
            _insert(conn, table, _row(table, seeded.a))


def test_migration_role_operator_insert_allowed(grounded_db: DbHandle, seeded: Seeded) -> None:
    """The operator path (ADR 0004): migration_role writes through its explicit policy."""
    url = grounded_db.url_for("migration_role", grounded_db.passwords["migration_role"])
    with psycopg.connect(url) as conn:
        _insert(conn, "tenant_email_domains", _row("tenant_email_domains", seeded.b))
        conn.rollback()

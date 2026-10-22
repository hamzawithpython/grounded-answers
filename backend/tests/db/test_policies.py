"""
Policy inventory (ADR 0004, ADR 0005, ADR 0007).

PostgreSQL checks INSERT with WITH CHECK only (USING does not apply to INSERT). So every INSERT
policy for app_user and worker_user must carry the tenant predicate in its WITH CHECK, and the
set of INSERT policies is pinned, so a new one cannot appear unreviewed.
"""

from collections.abc import Iterator

import psycopg
import pytest

from .database import DbHandle

pytestmark = pytest.mark.db

# The complete list of INSERT policies for the two app roles, with their table.
APP_INSERT_POLICIES = frozenset(
    {
        ("app_user_documents_insert", "documents"),
        ("app_user_document_versions_insert", "document_versions"),
        ("app_user_document_blobs_insert", "document_blobs"),
        ("app_user_audit_log_insert", "audit_log"),
        ("app_user_refresh_tokens_insert", "refresh_tokens"),
        ("app_user_password_reset_tokens_insert", "password_reset_tokens"),
        ("worker_audit_insert", "audit_log"),
    }
)


@pytest.fixture
def admin(grounded_db: DbHandle) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(grounded_db.admin_test_url())
    yield conn
    conn.close()


def _insert_policies(admin: psycopg.Connection) -> list[tuple[str, str, list[str], str | None]]:
    return [
        (name, table, list(roles), check)
        for name, table, roles, check in admin.execute(
            "SELECT policyname, tablename, roles, with_check FROM pg_policies "
            "WHERE schemaname = 'grounded' AND cmd = 'INSERT'"
        ).fetchall()
    ]


def test_every_app_and_worker_insert_policy_checks_the_tenant(admin: psycopg.Connection) -> None:
    """WITH CHECK must name the session tenant. A WITH CHECK of 'true' would be a bypass."""
    scoped = [
        (name, table, check)
        for name, table, roles, check in _insert_policies(admin)
        if {"app_user", "worker_user"} & set(roles)
    ]
    assert scoped, "no INSERT policies found for app_user or worker_user"
    for name, table, check in scoped:
        assert check is not None, f"{name} on {table} has no WITH CHECK"
        assert "current_setting('app.current_tenant'" in check, f"{name}: {check}"
        assert "tenant_id" in check, f"{name} does not compare tenant_id: {check}"


def test_app_and_worker_insert_policy_inventory_is_pinned(admin: psycopg.Connection) -> None:
    """The exact set of INSERT policies for app roles. Adding one is a reviewed change."""
    found = {
        (name, table)
        for name, table, roles, _ in _insert_policies(admin)
        if {"app_user", "worker_user"} & set(roles)
    }
    assert found == APP_INSERT_POLICIES, (
        f"extra: {found - APP_INSERT_POLICIES}  missing: {APP_INSERT_POLICIES - found}"
    )


def test_app_user_has_no_insert_policy_on_identity_or_membership_tables(
    admin: psycopg.Connection,
) -> None:
    """Employees are created only by create_tenant_user (decision B): no direct INSERT path."""
    for name, table, roles, _ in _insert_policies(admin):
        if "app_user" in roles:
            assert table not in {
                "users",
                "tenant_memberships",
                "tenants",
                "tenant_email_domains",
            }, f"{name} gives app_user a direct INSERT on {table}"

"""Table, column and function grants for app_user and worker_user.

Revision ID: 0002_roles_and_grants
Revises: 0001_tables
Create Date: 2026-10-22

Grants live in migrations (ADR 0006 amendment): versioned, reviewed, and applied to every
fresh test database by `alembic upgrade head`.

Principles:
- Column lists, not whole tables, wherever a column must stay hidden. app_user never reads
  users.password_hash; it can only write it (password change and reset paths).
- Append-only audit: app_user and worker_user have INSERT (and SELECT for app_user), never
  UPDATE or DELETE on audit_log.
- worker_user gets only what the purge job needs. RLS policies that narrow further are
  added in Step 5.
- The definer roles (auth_definer, retention_definer) get their grants with their functions
  in Step 5, when those functions exist.

Runs as migration_role. Keep expected_grants.json (tests/db) in step with this file.
"""

from collections.abc import Sequence

from alembic import op

revision = "0002_roles_and_grants"
down_revision = "0001_tables"
branch_labels = None
depends_on = None

UP: Sequence[str] = (
    # Connect and use the schema. Database CONNECT was granted in bootstrap_database.sql.
    "GRANT USAGE ON SCHEMA grounded TO app_user, worker_user",
    # --- app_user (web app) ---
    "GRANT SELECT (id, name, slug, status, created_at) ON grounded.tenants TO app_user",
    "GRANT SELECT (id, email, display_name, active, created_at) ON grounded.users TO app_user",
    "GRANT UPDATE (display_name, active) ON grounded.users TO app_user",
    # Write-only: the hash can be set (password change or reset), never read back.
    "GRANT UPDATE (password_hash) ON grounded.users TO app_user",
    # No INSERT on tenant_memberships: memberships are created only by create_tenant_user (0003).
    "GRANT SELECT ON grounded.tenant_memberships TO app_user",
    "GRANT UPDATE (role, active) ON grounded.tenant_memberships TO app_user",
    "GRANT SELECT, INSERT ON grounded.documents TO app_user",
    "GRANT UPDATE (title, current_version_id, deleted_at, purge_after) "
    "ON grounded.documents TO app_user",
    "GRANT SELECT, INSERT ON grounded.document_versions TO app_user",
    "GRANT UPDATE (status, failure_reason, retired_at, purge_after) "
    "ON grounded.document_versions TO app_user",
    "GRANT SELECT, INSERT ON grounded.audit_log TO app_user",
    "GRANT SELECT, INSERT ON grounded.refresh_tokens TO app_user",
    "GRANT UPDATE (used_at, revoked_at, replaced_by) ON grounded.refresh_tokens TO app_user",
    "GRANT SELECT, INSERT ON grounded.password_reset_tokens TO app_user",
    "GRANT UPDATE (used_at) ON grounded.password_reset_tokens TO app_user",
    # --- worker_user (purge job; row limits come from RLS in Step 5) ---
    "GRANT SELECT, DELETE ON grounded.documents TO worker_user",
    "GRANT UPDATE (current_version_id) ON grounded.documents TO worker_user",
    "GRANT SELECT, DELETE ON grounded.document_versions TO worker_user",
    "GRANT UPDATE (status) ON grounded.document_versions TO worker_user",
    "GRANT INSERT ON grounded.audit_log TO worker_user",
)

# REVOKE ALL ON a table does not remove column-level grants, so columns are named explicitly.
DOWN: Sequence[str] = (
    "REVOKE ALL ON grounded.audit_log FROM app_user, worker_user",
    "REVOKE ALL (status, failure_reason, retired_at, purge_after) "
    "ON grounded.document_versions FROM app_user",
    "REVOKE ALL (status) ON grounded.document_versions FROM worker_user",
    "REVOKE ALL ON grounded.document_versions FROM app_user, worker_user",
    "REVOKE ALL (title, current_version_id, deleted_at, purge_after) "
    "ON grounded.documents FROM app_user",
    "REVOKE ALL (current_version_id) ON grounded.documents FROM worker_user",
    "REVOKE ALL ON grounded.documents FROM app_user, worker_user",
    "REVOKE ALL (used_at) ON grounded.password_reset_tokens FROM app_user",
    "REVOKE ALL ON grounded.password_reset_tokens FROM app_user",
    "REVOKE ALL (used_at, revoked_at, replaced_by) ON grounded.refresh_tokens FROM app_user",
    "REVOKE ALL ON grounded.refresh_tokens FROM app_user",
    "REVOKE ALL (role, active) ON grounded.tenant_memberships FROM app_user",
    "REVOKE ALL ON grounded.tenant_memberships FROM app_user",
    "REVOKE ALL (password_hash) ON grounded.users FROM app_user",
    "REVOKE ALL (display_name, active) ON grounded.users FROM app_user",
    "REVOKE ALL ON grounded.users FROM app_user",
    "REVOKE ALL (id, name, slug, status, created_at) ON grounded.tenants FROM app_user",
    "REVOKE USAGE ON SCHEMA grounded FROM app_user, worker_user",
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

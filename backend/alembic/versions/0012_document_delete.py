"""Deleting a document: no restore for the guarded roles (ADR 0025, amends ADR 0026).

Revision ID: 0012_document_delete
Revises: 0011_stalled_version_retry
Create Date: 2026-10-25

ADR 0025 deletes a document by setting documents.deleted_at and purge_after, and it retires every version. Restore
is not supported in v1 (ARCHITECTURE 5.3). But app_user holds UPDATE on documents.deleted_at and purge_after (migration
0002), and nothing in the database stops it from clearing them. A cleared deleted_at would bring a deleted document
back, with its chunks, so this trigger refuses it.

The rule is narrow: once deleted_at is set, a guarded role may not clear it. Setting it, and every other column, is
unchanged. The trigger checks current_user, as the version guard does (migration 0010). migration_role is not
guarded. The function has a pinned search_path and no EXECUTE for PUBLIC, like the other trigger functions.
"""

from collections.abc import Sequence

from alembic import op

revision = "0012_document_delete"
down_revision = "0011_stalled_version_retry"
branch_labels = None
depends_on = None

UP: Sequence[str] = (
    """
    CREATE FUNCTION grounded.documents_no_restore() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, grounded
    AS $$
    BEGIN
      IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL
         AND current_user IN ('app_user', 'worker_user') THEN
        RAISE EXCEPTION '% may not restore a deleted document', current_user
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END;
    $$
    """,
    "REVOKE EXECUTE ON FUNCTION grounded.documents_no_restore() FROM PUBLIC",
    "CREATE TRIGGER documents_no_restore BEFORE UPDATE ON grounded.documents "
    "FOR EACH ROW EXECUTE FUNCTION grounded.documents_no_restore()",
)

DOWN: Sequence[str] = (
    "DROP TRIGGER documents_no_restore ON grounded.documents",
    "DROP FUNCTION grounded.documents_no_restore()",
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

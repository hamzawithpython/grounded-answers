"""Document bytes in PostgreSQL, behind an ObjectStore interface (ADR 0018).

Revision ID: 0007_document_blobs
Revises: 0006_worker_prune_grant
Create Date: 2026-10-23

One row per document version holds the file's bytes. The table is kept apart from documents and
document_versions, so normal document queries never read the bytes. The storage key is not stored
here: it is read from the version row (ADR 0018, decision P2 b).

Policies (ADR 0018, decision P1 b), enforced by RLS, not by the application:
- app_user: INSERT only, and only for a version in status 'queued' (the upload path). No SELECT,
  UPDATE or DELETE. The web app never reads or changes bytes in v1.
- worker_user: SELECT only for versions in 'queued', 'processing' or 'purging', and DELETE only for
  versions in 'purging' (the purge). 'purging' is in SELECT because PostgreSQL requires a row to be
  visible through a SELECT policy before DELETE can remove it (ADR 0018, amendment). Bytes of
  'ready', 'failed' and 'retired' versions stay unreadable to the worker. A re-ingestion of ready
  documents needs a new migration that widens SELECT. It is never loosened silently.

This is a new migration, not an edit of 0006, because 0006 is merged (CLAUDE.md, migrations rule).
"""

from collections.abc import Sequence

from alembic import op

revision = "0007_document_blobs"
down_revision = "0006_worker_prune_grant"
branch_labels = None
depends_on = None

# The tenant predicate, as in 0003: NULL when no tenant is set, so no row matches (fail closed).
CUR = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"

# The version a blob belongs to, in the same tenant, with the status the policy allows.
VERSION_WITH_STATUS = (
    "EXISTS (SELECT 1 FROM grounded.document_versions AS v "
    "WHERE v.tenant_id = document_blobs.tenant_id AND v.id = document_blobs.version_id "
    "AND v.status {status})"
)

UP: Sequence[str] = (
    """
    CREATE TABLE grounded.document_blobs (
      tenant_id uuid NOT NULL
        REFERENCES grounded.tenants (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      version_id uuid NOT NULL,
      content bytea NOT NULL,
      byte_size integer NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT document_blobs_pkey PRIMARY KEY (tenant_id, version_id),
      CONSTRAINT document_blobs_version_fk FOREIGN KEY (tenant_id, version_id)
        REFERENCES grounded.document_versions (tenant_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      CONSTRAINT document_blobs_size_check CHECK (byte_size BETWEEN 1 AND 10485760),
      CONSTRAINT document_blobs_length_check CHECK (octet_length(content) = byte_size)
    )
    """,
    "ALTER TABLE grounded.document_blobs ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.document_blobs FORCE ROW LEVEL SECURITY",
    # ---- app_user: insert bytes for a queued version of its own tenant, nothing else ----
    "CREATE POLICY app_user_document_blobs_insert ON grounded.document_blobs "
    f"FOR INSERT TO app_user WITH CHECK (tenant_id = {CUR} AND "
    + VERSION_WITH_STATUS.format(status="= 'queued'")
    + ")",
    # ---- worker_user: read bytes for ingestion, delete bytes for the purge ----
    # SELECT must also cover 'purging': PostgreSQL requires a row to be visible through a SELECT
    # policy before DELETE can remove it. Without 'purging' here, the purge deletes 0 rows silently.
    "CREATE POLICY worker_document_blobs_select ON grounded.document_blobs "
    f"FOR SELECT TO worker_user USING (tenant_id = {CUR} AND "
    + VERSION_WITH_STATUS.format(status="IN ('queued', 'processing', 'purging')")
    + ")",
    "CREATE POLICY worker_document_blobs_delete ON grounded.document_blobs "
    f"FOR DELETE TO worker_user USING (tenant_id = {CUR} AND "
    + VERSION_WITH_STATUS.format(status="= 'purging'")
    + ")",
    # ---- migration_role (operator, ADR 0004): explicit, as on the other tenant tables ----
    "CREATE POLICY migration_role_document_blobs_all ON grounded.document_blobs "
    "FOR ALL TO migration_role USING (true) WITH CHECK (true)",
    # ---- grants: the least that the policies above need ----
    "GRANT INSERT ON grounded.document_blobs TO app_user",
    "GRANT SELECT, DELETE ON grounded.document_blobs TO worker_user",
)

DOWN: Sequence[str] = ("DROP TABLE grounded.document_blobs",)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

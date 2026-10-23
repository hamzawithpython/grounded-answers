"""Ingestion lease and fencing token on document versions (ADR 0022).

Revision ID: 0009_ingestion_lease
Revises: 0008_chunks
Create Date: 2026-10-23

A worker that claims a version sets processing_started_at (the lease start) and processing_token (a new uuid).
A take-over after the lease expires sets a new token. The final write of an ingestion requires the token it
claimed with, so a worker that lost its lease writes nothing (ADR 0022, decision D1).

Policies for worker_user (enforced by RLS):
- worker_versions_claim: queued -> processing, and nothing else. This is the only way a queued version
  leaves queued.
- worker_versions_process: a version that is processing may be updated, and its status may only stay
  processing (lease take-over) or become ready or failed (finish). failure_reason is written here too.
- worker_documents_set_current: set documents.current_version_id on a live document whose current version
  is unset, and only to a version that is ready. This is the first version only (replacement is Step 7).

The approved wording limited UPDATE to versions already in processing. That cannot cover the claim, because
the claimed row is still queued, so the claim has its own policy above.

The policies cannot express "from queued to processing" or "from processing to ready": a WITH CHECK sees only the
new row, and permissive policies are combined with OR. So the transitions are enforced by a trigger,
document_versions_worker_transitions, which allows only the worker's steps for worker_user. It is ADR 0022's
transition guard, and it is a new security control, approved separately from the policies.
"""

from collections.abc import Sequence

from alembic import op

revision = "0009_ingestion_lease"
down_revision = "0008_chunks"
branch_labels = None
depends_on = None

CUR = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"

# The document's current version is set only when it is ready and belongs to the document (first version only).
VERSION_IS_READY_FOR_DOCUMENT = (
    "EXISTS (SELECT 1 FROM grounded.document_versions AS v "
    "WHERE v.tenant_id = documents.tenant_id AND v.id = documents.current_version_id "
    "AND v.document_id = documents.id AND v.status = 'ready')"
)

UP: Sequence[str] = (
    "ALTER TABLE grounded.document_versions "
    "ADD COLUMN processing_started_at timestamptz NULL, "
    "ADD COLUMN processing_token uuid NULL, "
    "ADD CONSTRAINT document_versions_processing_lease_check CHECK ("
    "status <> 'processing' OR (processing_started_at IS NOT NULL AND processing_token IS NOT NULL))",
    # ---- worker_user: claim, process, finish ----
    "CREATE POLICY worker_versions_claim ON grounded.document_versions FOR UPDATE TO worker_user "
    f"USING (tenant_id = {CUR} AND status = 'queued') "
    f"WITH CHECK (tenant_id = {CUR} AND status = 'processing')",
    "CREATE POLICY worker_versions_process ON grounded.document_versions FOR UPDATE TO worker_user "
    f"USING (tenant_id = {CUR} AND status = 'processing') "
    f"WITH CHECK (tenant_id = {CUR} AND status IN ('processing', 'ready', 'failed'))",
    # ---- worker_user: first current version, only a ready one ----
    "CREATE POLICY worker_documents_set_current ON grounded.documents FOR UPDATE TO worker_user "
    f"USING (tenant_id = {CUR} AND deleted_at IS NULL AND current_version_id IS NULL) "
    f"WITH CHECK (tenant_id = {CUR} AND deleted_at IS NULL AND "
    + VERSION_IS_READY_FOR_DOCUMENT
    + ")",
    "GRANT UPDATE (processing_started_at, processing_token, failure_reason) "
    "ON grounded.document_versions TO worker_user",
    # ---- The transition guard. The policies above cannot see the old status in a WITH CHECK, so
    # the permissive policies widen what a worker may write: without this trigger, a worker could move
    # a queued version straight to ready (a test showed it). The trigger allows only the worker's steps.
    """
    CREATE FUNCTION grounded.document_versions_worker_transitions() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, grounded
    AS $$
    BEGIN
      IF current_user <> 'worker_user' OR NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NEW;
      END IF;
      IF (OLD.status, NEW.status) IN (
           ('queued', 'processing'),
           ('processing', 'ready'),
           ('processing', 'failed'),
           ('retired', 'purging')) THEN
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'worker_user may not move a version from % to %', OLD.status, NEW.status
        USING ERRCODE = 'insufficient_privilege';
    END;
    $$
    """,
    # Functions are granted to PUBLIC by default. A trigger function is never called by a role (ADR 0022).
    "REVOKE EXECUTE ON FUNCTION grounded.document_versions_worker_transitions() FROM PUBLIC",
    "CREATE TRIGGER document_versions_worker_transitions BEFORE UPDATE ON grounded.document_versions "
    "FOR EACH ROW EXECUTE FUNCTION grounded.document_versions_worker_transitions()",
)

DOWN: Sequence[str] = (
    "DROP TRIGGER document_versions_worker_transitions ON grounded.document_versions",
    "DROP FUNCTION grounded.document_versions_worker_transitions()",
    "REVOKE UPDATE (processing_started_at, processing_token, failure_reason) "
    "ON grounded.document_versions FROM worker_user",
    "DROP POLICY worker_documents_set_current ON grounded.documents",
    "DROP POLICY worker_versions_process ON grounded.document_versions",
    "DROP POLICY worker_versions_claim ON grounded.document_versions",
    "ALTER TABLE grounded.document_versions "
    "DROP CONSTRAINT document_versions_processing_lease_check, "
    "DROP COLUMN processing_token, DROP COLUMN processing_started_at",
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

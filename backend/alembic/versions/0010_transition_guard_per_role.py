"""Per-role transition guard, and app_user loses UPDATE on documents.current_version_id (ADR 0026).

Revision ID: 0010_transition_guard_per_role
Revises: 0009_ingestion_lease
Create Date: 2026-10-24

The trigger from migration 0009 guarded worker_user only. app_user could still move any version to any status
(its grant covers the status column, and its RLS policy checks only the tenant), and could point any document at
any version of its tenant (ADR 0026, context).

The trigger now keeps one table of allowed transitions per guarded role:
- worker_user: the claim, the process and the finish, and the purge (unchanged from 0009).
- app_user: retire a queued, processing, ready or failed version (delete, ADR 0025). Nothing else.

A change to any other status, by a guarded role, raises insufficient_privilege. A write that keeps the same status
is always allowed, and the policies still decide it. migration_role is not guarded: it runs migrations from the
shell only (ADR 0004).

swap_definer (ADR 0024) is not listed yet. It is added with its own function, in PR 6, because the role does not
exist before then.

app_user also loses UPDATE on documents.current_version_id. Only the worker sets it, and only when it is NULL
(ingest/pipeline.py). The replacement swap moves it through a definer function instead (ADR 0024).
"""

from collections.abc import Sequence

from alembic import op

revision = "0010_transition_guard_per_role"
down_revision = "0009_ingestion_lease"
branch_labels = None
depends_on = None

GUARD_FUNCTION_UP = """
CREATE OR REPLACE FUNCTION grounded.document_versions_worker_transitions() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, grounded
AS $$
BEGIN
  IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NEW;
  END IF;
  IF current_user = 'worker_user' AND (OLD.status, NEW.status) IN (
       ('queued', 'processing'),
       ('processing', 'ready'),
       ('processing', 'failed'),
       ('retired', 'purging')) THEN
    RETURN NEW;
  END IF;
  IF current_user = 'app_user' AND (OLD.status, NEW.status) IN (
       ('queued', 'retired'),
       ('processing', 'retired'),
       ('ready', 'retired'),
       ('failed', 'retired')) THEN
    RETURN NEW;
  END IF;
  IF current_user IN ('worker_user', 'app_user') THEN
    RAISE EXCEPTION '% may not move a version from % to %', current_user, OLD.status, NEW.status
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END;
$$
"""

# The function as migration 0009 left it. The downgrade restores it exactly.
GUARD_FUNCTION_0009 = """
CREATE OR REPLACE FUNCTION grounded.document_versions_worker_transitions() RETURNS trigger
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
"""

UP: Sequence[str] = (
    GUARD_FUNCTION_UP,
    "REVOKE UPDATE (current_version_id) ON grounded.documents FROM app_user",
)

DOWN: Sequence[str] = (
    "GRANT UPDATE (current_version_id) ON grounded.documents TO app_user",
    GUARD_FUNCTION_0009,
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

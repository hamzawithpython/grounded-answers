"""Replacing a document version: the document lock, the swap definer and its transitions (ADR 0024).

Revision ID: 0013_replace_and_swap
Revises: 0012_document_delete
Create Date: 2026-10-25

The document row is the one serialization point for a document's versions (ADR 0024, amended): version numbers are
allocated under it, the swap compares version numbers under it, and a delete takes it. Its lock rules, from the
PostgreSQL documentation (CREATE POLICY, Table "Policy Commands" and the rows for SELECT FOR UPDATE/SHARE): a row
locked by SELECT ... FOR UPDATE is filtered by the USING expressions of SELECT and UPDATE policies, and no WITH CHECK
expression applies to the lock. The locking clause needs UPDATE privilege on at least one column (SELECT reference).

So worker_user gets a lock-only policy: FOR UPDATE on a live document of its tenant, with WITH CHECK (false). The lock
succeeds, and every UPDATE is still refused: the WITH CHECK of a permissive policy must hold for the new row, and
none holds. The purge policy (worker_documents_update, migration 0003) is unchanged.

The finish no longer sets the current version. grounded.swap_current_version does, for every version, under the lock.
So worker_documents_set_current (migration 0009) is removed. worker_user keeps UPDATE (current_version_id), because
the lock needs an UPDATE privilege and the purge clears that column.

swap_definer (created by bootstrap_definer_roles.sql) owns the function and may make exactly two status moves,
ready to retired, for the old current version and for a superseded finisher. The function refuses a version of
another document or tenant, and a version that is not ready.

The retention period in the function is TEMPORARY_RETENTION_DAYS (30), until PR 7 adds DOCUMENT_RETENTION_DAYS.
"""

from collections.abc import Sequence

from alembic import op

revision = "0013_replace_and_swap"
down_revision = "0012_document_delete"
branch_labels = None
depends_on = None

CUR = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"

# The swap, run as swap_definer. One transaction: the caller's tenant is the session tenant, the document is locked, and
# the pointer, the retirements and the audit row commit together.
SWAP_FUNCTION = """
CREATE FUNCTION grounded.swap_current_version(p_document_id uuid, p_version_id uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, grounded
AS $$
DECLARE
  tenant uuid := NULLIF(current_setting('app.current_tenant', true), '')::uuid;
  current_id uuid;
  current_no integer;
  finisher_no integer;
  finisher_status text;
  finisher_document uuid;
  finisher_tenant uuid;
  outcome text;
  retired_count integer := 0;
BEGIN
  IF tenant IS NULL THEN
    RAISE EXCEPTION 'the swap needs a tenant context' USING ERRCODE = 'insufficient_privilege';
  END IF;
  -- One lock per document. Replacement, delete and this swap all wait here (ADR 0024).
  SELECT d.current_version_id INTO current_id
  FROM grounded.documents AS d
  WHERE d.id = p_document_id AND d.tenant_id = tenant AND d.deleted_at IS NULL
  FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'the document is not live in this tenant' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT v.version_no, v.status, v.document_id, v.tenant_id
  INTO finisher_no, finisher_status, finisher_document, finisher_tenant
  FROM grounded.document_versions AS v
  WHERE v.id = p_version_id;
  IF NOT FOUND OR finisher_document <> p_document_id OR finisher_tenant <> tenant THEN
    RAISE EXCEPTION 'the version does not belong to this document' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF finisher_status <> 'ready' THEN
    RAISE EXCEPTION 'only a ready version can be swapped in' USING ERRCODE = 'check_violation';
  END IF;
  IF current_id IS NULL THEN
    current_no := NULL;
  ELSE
    SELECT v.version_no INTO current_no FROM grounded.document_versions AS v WHERE v.id = current_id;
  END IF;

  IF current_no IS NULL OR finisher_no > current_no THEN
    IF current_id IS NOT NULL THEN
      UPDATE grounded.document_versions
      SET status = 'retired', retired_at = now(), purge_after = now() + make_interval(days => 30)
      WHERE id = current_id AND tenant_id = tenant;
      GET DIAGNOSTICS retired_count = ROW_COUNT;
      IF retired_count <> 1 THEN
        RAISE EXCEPTION 'the old current version was not retired' USING ERRCODE = 'check_violation';
      END IF;
    END IF;
    UPDATE grounded.documents SET current_version_id = p_version_id
    WHERE id = p_document_id AND tenant_id = tenant;
    outcome := 'current';
  ELSE
    UPDATE grounded.document_versions
    SET status = 'retired', retired_at = now(), purge_after = now() + make_interval(days => 30)
    WHERE id = p_version_id AND tenant_id = tenant;
    retired_count := 1;
    outcome := 'superseded';
  END IF;

  INSERT INTO grounded.audit_log (id, tenant_id, actor_user_id, action, target_type, target_id, detail)
  VALUES (pg_catalog.gen_random_uuid(), tenant, NULL, 'document.swap', 'document', p_document_id,
          pg_catalog.jsonb_build_object('outcome', outcome, 'retired', retired_count));
  RETURN outcome;
END;
$$
"""

# The guard from 0010, with swap_definer added. Its only moves: ready to retired.
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
  IF current_user = 'swap_definer' AND (OLD.status, NEW.status) IN (
       ('ready', 'retired')) THEN
    RETURN NEW;
  END IF;
  IF current_user IN ('worker_user', 'app_user', 'swap_definer') THEN
    RAISE EXCEPTION '% may not move a version from % to %', current_user, OLD.status, NEW.status
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END;
$$
"""

# The guard as migration 0010 left it, restored exactly by the downgrade.
GUARD_FUNCTION_0010 = """
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

# The set-current policy from migration 0009, restored by the downgrade.
SET_CURRENT_POLICY_0009 = """
CREATE POLICY worker_documents_set_current ON grounded.documents FOR UPDATE TO worker_user
USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
       AND deleted_at IS NULL AND current_version_id IS NULL)
WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
            AND deleted_at IS NULL AND EXISTS (SELECT 1 FROM grounded.document_versions AS v
            WHERE v.tenant_id = documents.tenant_id AND v.id = documents.current_version_id
            AND v.document_id = documents.id AND v.status = 'ready'))
"""

UP: Sequence[str] = (
    # ---- worker_user: lock a live document, and nothing else (see the module docstring) ----
    "DROP POLICY worker_documents_set_current ON grounded.documents",
    "CREATE POLICY worker_documents_lock ON grounded.documents FOR UPDATE TO worker_user "
    f"USING (tenant_id = {CUR} AND deleted_at IS NULL) WITH CHECK (false)",
    # ---- swap_definer: read and change the rows the swap touches, in the session tenant only ----
    "CREATE POLICY swap_definer_documents_select ON grounded.documents FOR SELECT TO swap_definer "
    f"USING (tenant_id = {CUR})",
    "CREATE POLICY swap_definer_documents_update ON grounded.documents FOR UPDATE TO swap_definer "
    f"USING (tenant_id = {CUR} AND deleted_at IS NULL) WITH CHECK (tenant_id = {CUR})",
    "CREATE POLICY swap_definer_versions_select ON grounded.document_versions FOR SELECT TO swap_definer "
    f"USING (tenant_id = {CUR})",
    "CREATE POLICY swap_definer_versions_update ON grounded.document_versions FOR UPDATE TO swap_definer "
    f"USING (tenant_id = {CUR}) WITH CHECK (tenant_id = {CUR})",
    "CREATE POLICY swap_definer_audit_insert ON grounded.audit_log FOR INSERT TO swap_definer "
    f"WITH CHECK (tenant_id = {CUR})",
    "GRANT USAGE ON SCHEMA grounded TO swap_definer",
    "GRANT SELECT (id, tenant_id, deleted_at, current_version_id) ON grounded.documents TO swap_definer",
    "GRANT UPDATE (current_version_id) ON grounded.documents TO swap_definer",
    "GRANT SELECT (id, tenant_id, document_id, version_no, status) ON grounded.document_versions TO swap_definer",
    "GRANT UPDATE (status, retired_at, purge_after) ON grounded.document_versions TO swap_definer",
    "GRANT INSERT ON grounded.audit_log TO swap_definer",
    # ---- the swap function: worker_user may execute it and nothing else (ADR 0024) ----
    SWAP_FUNCTION,
    "REVOKE ALL ON FUNCTION grounded.swap_current_version(uuid, uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION grounded.swap_current_version(uuid, uuid) TO worker_user",
    # Owner handover, as in migration 0011: CREATE on the schema only for the change.
    "GRANT CREATE ON SCHEMA grounded TO swap_definer",
    "ALTER FUNCTION grounded.swap_current_version(uuid, uuid) OWNER TO swap_definer",
    "REVOKE CREATE ON SCHEMA grounded FROM swap_definer",
    # ---- the guard: swap_definer may make only ready to retired ----
    GUARD_FUNCTION_UP,
)

DOWN: Sequence[str] = (
    GUARD_FUNCTION_0010,
    "DROP FUNCTION grounded.swap_current_version(uuid, uuid)",
    "REVOKE UPDATE (status, retired_at, purge_after) ON grounded.document_versions FROM swap_definer",
    "REVOKE SELECT (id, tenant_id, document_id, version_no, status) ON grounded.document_versions FROM swap_definer",
    "REVOKE INSERT ON grounded.audit_log FROM swap_definer",
    "REVOKE UPDATE (current_version_id) ON grounded.documents FROM swap_definer",
    "REVOKE SELECT (id, tenant_id, deleted_at, current_version_id) ON grounded.documents FROM swap_definer",
    "REVOKE USAGE ON SCHEMA grounded FROM swap_definer",
    "DROP POLICY swap_definer_audit_insert ON grounded.audit_log",
    "DROP POLICY swap_definer_versions_update ON grounded.document_versions",
    "DROP POLICY swap_definer_versions_select ON grounded.document_versions",
    "DROP POLICY swap_definer_documents_update ON grounded.documents",
    "DROP POLICY swap_definer_documents_select ON grounded.documents",
    "DROP POLICY worker_documents_lock ON grounded.documents",
    SET_CURRENT_POLICY_0009,
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

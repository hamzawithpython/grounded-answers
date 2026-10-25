"""The purge: retention from configuration, and a narrow definer that enqueues the purge jobs (ADR 0027, ADR 0007).

Revision ID: 0014_purge
Revises: 0013_replace_and_swap
Create Date: 2026-10-25

Three changes.

1. grounded.swap_current_version takes the retention period as a third argument, p_retention_days, validated to 1 to
   365 (DOCUMENT_RETENTION_DAYS). The temporary 30-day constant of migration 0013 is gone. The worker passes the setting.
   The function is replaced, because its signature changes. Its owner and grants are set again.

2. purge_definer (NOLOGIN, created by bootstrap_definer_roles.sql) owns grounded.enqueue_purge_jobs(). It reads the due
   items across tenants, and returns only counts. For each due item it defers one grounded.purge_item job. The queue is
   the only thing it writes. Its privileges are the same queue grants the enqueueing role holds, and two read policies
   that return ids and timestamps, never content. worker_user gets EXECUTE on the function and nothing else.

   Due items: a deleted document whose purge_after has passed (kind 'document'); a retired version of a live document
   whose purge_after has passed (kind 'version'). A retired version of a deleted document is covered by its document's
   job, so it gets no job of its own.

3. The duplicate rule is the one of migration 0011: a queued job with the same queueing lock is counted and skipped, per
   item, so one waiting job never stops the sweep.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0014_purge"
down_revision = "0013_replace_and_swap"
branch_labels = None
depends_on = None


def _load_0013() -> ModuleType:
    """The swap of migration 0013, loaded from its file, so the downgrade can restore it exactly."""
    path = Path(__file__).with_name("0013_replace_and_swap.py")
    spec = importlib.util.spec_from_file_location("migration_0013_replace_and_swap", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration 0013 cannot be loaded for the downgrade")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The swap with the retention period as an argument. The rest is the function of migration 0013.
SWAP_FUNCTION_3ARG = """
CREATE FUNCTION grounded.swap_current_version(p_document_id uuid, p_version_id uuid, p_retention_days integer)
RETURNS text
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
  IF p_retention_days IS NULL OR p_retention_days NOT BETWEEN 1 AND 365 THEN
    RAISE EXCEPTION 'the retention period must be between 1 and 365 days' USING ERRCODE = 'invalid_parameter_value';
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
      SET status = 'retired', retired_at = now(), purge_after = now() + make_interval(days => p_retention_days)
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
    SET status = 'retired', retired_at = now(), purge_after = now() + make_interval(days => p_retention_days)
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

ENQUEUE_PURGE_FUNCTION = """
CREATE FUNCTION grounded.enqueue_purge_jobs() RETURNS TABLE (out_enqueued integer, out_already_waiting integer)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, grounded, queue
AS $$
DECLARE
  r record;
  enqueued integer := 0;
  already_waiting integer := 0;
BEGIN
  FOR r IN
    SELECT 'document'::text AS kind, d.tenant_id, d.id AS item_id
    FROM grounded.documents AS d
    WHERE d.deleted_at IS NOT NULL AND d.purge_after <= pg_catalog.now()
    UNION ALL
    SELECT 'version'::text, v.tenant_id, v.id
    FROM grounded.document_versions AS v
    JOIN grounded.documents AS d ON d.tenant_id = v.tenant_id AND d.id = v.document_id
    WHERE v.status = 'retired' AND v.purge_after <= pg_catalog.now() AND d.deleted_at IS NULL
  LOOP
    BEGIN
      PERFORM queue.procrastinate_defer_jobs_v1(ARRAY[ROW(
        'default',
        'grounded.purge_item',
        0,
        NULL,
        'purge:' || r.kind || ':' || r.item_id::text,
        pg_catalog.jsonb_build_object(
          'tenant_id', r.tenant_id::text,
          'kind', r.kind,
          'item_id', r.item_id::text),
        NULL::timestamptz
      )::queue.procrastinate_job_to_defer_v1]);
      enqueued := enqueued + 1;
    EXCEPTION WHEN unique_violation THEN
      already_waiting := already_waiting + 1;
    END;
  END LOOP;
  RETURN QUERY SELECT enqueued, already_waiting;
END;
$$
"""

UP: Sequence[str] = (
    # ---- 1. the swap takes the retention period: replace the function (the signature changes) ----
    "DROP FUNCTION grounded.swap_current_version(uuid, uuid)",
    SWAP_FUNCTION_3ARG,
    "REVOKE ALL ON FUNCTION grounded.swap_current_version(uuid, uuid, integer) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION grounded.swap_current_version(uuid, uuid, integer) TO worker_user",
    "GRANT CREATE ON SCHEMA grounded TO swap_definer",
    "ALTER FUNCTION grounded.swap_current_version(uuid, uuid, integer) OWNER TO swap_definer",
    "REVOKE CREATE ON SCHEMA grounded FROM swap_definer",
    # ---- 2. purge_definer: read ids and timestamps across tenants, and defer purge jobs ----
    "CREATE POLICY purge_definer_documents_select ON grounded.documents FOR SELECT TO purge_definer USING (true)",
    "CREATE POLICY purge_definer_versions_select ON grounded.document_versions FOR SELECT TO purge_definer "
    "USING (true)",
    "GRANT USAGE ON SCHEMA grounded TO purge_definer",
    "GRANT SELECT (id, tenant_id, deleted_at, purge_after) ON grounded.documents TO purge_definer",
    "GRANT SELECT (id, tenant_id, document_id, status, purge_after) ON grounded.document_versions TO purge_definer",
    "GRANT USAGE ON SCHEMA queue TO purge_definer",
    "GRANT EXECUTE ON FUNCTION queue.procrastinate_defer_jobs_v1(queue.procrastinate_job_to_defer_v1[]) "
    "TO purge_definer",
    "GRANT USAGE ON TYPE queue.procrastinate_job_to_defer_v1 TO purge_definer",
    "GRANT INSERT (queue_name, task_name, priority, lock, queueing_lock, args, scheduled_at) "
    "ON queue.procrastinate_jobs TO purge_definer",
    "GRANT USAGE ON SEQUENCE queue.procrastinate_jobs_id_seq TO purge_definer",
    "GRANT SELECT (id) ON queue.procrastinate_jobs TO purge_definer",
    "GRANT INSERT (job_id, type, at) ON queue.procrastinate_events TO purge_definer",
    "GRANT USAGE ON SEQUENCE queue.procrastinate_events_id_seq TO purge_definer",
    ENQUEUE_PURGE_FUNCTION,
    "REVOKE ALL ON FUNCTION grounded.enqueue_purge_jobs() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION grounded.enqueue_purge_jobs() TO worker_user",
    "GRANT CREATE ON SCHEMA grounded TO purge_definer",
    "ALTER FUNCTION grounded.enqueue_purge_jobs() OWNER TO purge_definer",
    "REVOKE CREATE ON SCHEMA grounded FROM purge_definer",
)

DOWN: Sequence[str] = (
    "DROP FUNCTION grounded.enqueue_purge_jobs()",
    "REVOKE INSERT (job_id, type, at) ON queue.procrastinate_events FROM purge_definer",
    "REVOKE USAGE ON SEQUENCE queue.procrastinate_events_id_seq FROM purge_definer",
    "REVOKE SELECT (id) ON queue.procrastinate_jobs FROM purge_definer",
    "REVOKE USAGE ON SEQUENCE queue.procrastinate_jobs_id_seq FROM purge_definer",
    "REVOKE INSERT (queue_name, task_name, priority, lock, queueing_lock, args, scheduled_at) "
    "ON queue.procrastinate_jobs FROM purge_definer",
    "REVOKE USAGE ON TYPE queue.procrastinate_job_to_defer_v1 FROM purge_definer",
    "REVOKE EXECUTE ON FUNCTION queue.procrastinate_defer_jobs_v1(queue.procrastinate_job_to_defer_v1[]) "
    "FROM purge_definer",
    "REVOKE USAGE ON SCHEMA queue FROM purge_definer",
    "REVOKE SELECT (id, tenant_id, document_id, status, purge_after) ON grounded.document_versions "
    "FROM purge_definer",
    "REVOKE SELECT (id, tenant_id, deleted_at, purge_after) ON grounded.documents FROM purge_definer",
    "REVOKE USAGE ON SCHEMA grounded FROM purge_definer",
    "DROP POLICY purge_definer_versions_select ON grounded.document_versions",
    "DROP POLICY purge_definer_documents_select ON grounded.documents",
    "DROP FUNCTION grounded.swap_current_version(uuid, uuid, integer)",
    # The swap of migration 0013 (two arguments, a 30-day constant), restored from its file.
    _load_0013().SWAP_FUNCTION,
    "REVOKE ALL ON FUNCTION grounded.swap_current_version(uuid, uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION grounded.swap_current_version(uuid, uuid) TO worker_user",
    "GRANT CREATE ON SCHEMA grounded TO swap_definer",
    "ALTER FUNCTION grounded.swap_current_version(uuid, uuid) OWNER TO swap_definer",
    "REVOKE CREATE ON SCHEMA grounded FROM swap_definer",
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

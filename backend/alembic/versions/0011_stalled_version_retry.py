"""The stalled-version retry: a security-definer function that defers the retry jobs (ADR 0023, amended).

Revision ID: 0011_stalled_version_retry
Revises: 0010_transition_guard_per_role
Create Date: 2026-10-24

A worker killed hard leaves a version in processing, with no job to retry it (ADR 0022, D5). The periodic sweep
needs to see such versions in every tenant, and worker_user is tenant-scoped (ADR 0017). So the sweep is one
SECURITY DEFINER function, grounded.enqueue_stalled_ingest, owned by retry_definer. It finds the versions whose
lease has expired, and defers one ingest job for each. It follows the rules in SECURITY.md, "Definer functions":
pinned search_path, no EXECUTE for PUBLIC, a NOLOGIN owner.

worker_user gets EXECUTE on this function, and nothing else for enqueueing. A worker cannot enqueue an arbitrary
task. The task name is fixed inside the function.

Duplicates: the queue has a partial unique index on queueing_lock while a job is todo. A duplicate insert raises
unique_violation, and that would abort a batch insert. So each version is deferred in its own block, and a version
that already has a waiting job is counted and skipped. The sweep never stops for one version.

The lock text is "ingest:<version_id>", the same as jobs/stalled.py. The lease test is the one the claim uses
(ingest/pipeline.py): processing_started_at at or before now() minus the lease.

The role retry_definer is created by scripts/bootstrap_definer_roles.sql, not by this migration. Run that file
before this migration on every database (RUNBOOK, step 4b).
"""

from collections.abc import Sequence

from alembic import op

revision = "0011_stalled_version_retry"
down_revision = "0010_transition_guard_per_role"
branch_labels = None
depends_on = None

# plpgsql, not sql: it loops, catches the duplicate per version, and refuses a bad lease. The out_ prefixes keep the
# output names apart from the local variables (plpgsql would otherwise report them as ambiguous).
# The task name is written out here, so a caller can never choose another task.
ENQUEUE_FUNCTION = """
CREATE FUNCTION grounded.enqueue_stalled_ingest(p_lease_seconds integer)
RETURNS TABLE (out_enqueued integer, out_already_waiting integer)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, grounded, queue
AS $$
DECLARE
  r record;
  enqueued integer := 0;
  already_waiting integer := 0;
BEGIN
  IF p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 120 AND 7200 THEN
    RAISE EXCEPTION 'lease seconds must be between 120 and 7200'
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  FOR r IN
    SELECT v.tenant_id, v.document_id, v.id AS version_id
    FROM grounded.document_versions AS v
    WHERE v.status = 'processing'
      AND v.processing_started_at <= pg_catalog.now() - pg_catalog.make_interval(secs => p_lease_seconds)
  LOOP
    BEGIN
      PERFORM queue.procrastinate_defer_jobs_v1(ARRAY[ROW(
        'default',
        'grounded.ingest_document',
        0,
        NULL,
        'ingest:' || r.version_id::text,
        pg_catalog.jsonb_build_object(
          'tenant_id', r.tenant_id::text,
          'document_id', r.document_id::text,
          'version_id', r.version_id::text),
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
    # ---- retry_definer: read the scan's columns across tenants (a policy with USING true) ----
    "CREATE POLICY retry_definer_versions_select ON grounded.document_versions "
    "FOR SELECT TO retry_definer USING (true)",
    "GRANT USAGE ON SCHEMA grounded TO retry_definer",
    "GRANT SELECT (id, tenant_id, document_id, status, processing_started_at) "
    "ON grounded.document_versions TO retry_definer",
    # ---- retry_definer: defer ingest jobs. The same grants app_user holds for enqueueing (migration 0005),
    # so the defer runs under the owner's rights, and nothing more. ----
    "GRANT USAGE ON SCHEMA queue TO retry_definer",
    "GRANT EXECUTE ON FUNCTION queue.procrastinate_defer_jobs_v1(queue.procrastinate_job_to_defer_v1[]) "
    "TO retry_definer",
    "GRANT USAGE ON TYPE queue.procrastinate_job_to_defer_v1 TO retry_definer",
    "GRANT INSERT (queue_name, task_name, priority, lock, queueing_lock, args, scheduled_at) "
    "ON queue.procrastinate_jobs TO retry_definer",
    "GRANT USAGE ON SEQUENCE queue.procrastinate_jobs_id_seq TO retry_definer",
    "GRANT SELECT (id) ON queue.procrastinate_jobs TO retry_definer",
    # The status trigger writes one event row per job, as the caller (retry_definer).
    "GRANT INSERT (job_id, type, at) ON queue.procrastinate_events TO retry_definer",
    "GRANT USAGE ON SEQUENCE queue.procrastinate_events_id_seq TO retry_definer",
    ENQUEUE_FUNCTION,
    "REVOKE ALL ON FUNCTION grounded.enqueue_stalled_ingest(integer) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION grounded.enqueue_stalled_ingest(integer) TO worker_user",
    # Owner handover, as in migration 0003: CREATE on the schema only for the change.
    "GRANT CREATE ON SCHEMA grounded TO retry_definer",
    "ALTER FUNCTION grounded.enqueue_stalled_ingest(integer) OWNER TO retry_definer",
    "REVOKE CREATE ON SCHEMA grounded FROM retry_definer",
)

DOWN: Sequence[str] = (
    "DROP FUNCTION grounded.enqueue_stalled_ingest(integer)",
    "REVOKE INSERT (job_id, type, at) ON queue.procrastinate_events FROM retry_definer",
    "REVOKE USAGE ON SEQUENCE queue.procrastinate_events_id_seq FROM retry_definer",
    "REVOKE SELECT (id) ON queue.procrastinate_jobs FROM retry_definer",
    "REVOKE USAGE ON SEQUENCE queue.procrastinate_jobs_id_seq FROM retry_definer",
    "REVOKE INSERT (queue_name, task_name, priority, lock, queueing_lock, args, scheduled_at) "
    "ON queue.procrastinate_jobs FROM retry_definer",
    "REVOKE USAGE ON TYPE queue.procrastinate_job_to_defer_v1 FROM retry_definer",
    "REVOKE EXECUTE ON FUNCTION queue.procrastinate_defer_jobs_v1(queue.procrastinate_job_to_defer_v1[]) "
    "FROM retry_definer",
    "REVOKE USAGE ON SCHEMA queue FROM retry_definer",
    "REVOKE SELECT (id, tenant_id, document_id, status, processing_started_at) "
    "ON grounded.document_versions FROM retry_definer",
    "REVOKE USAGE ON SCHEMA grounded FROM retry_definer",
    "DROP POLICY retry_definer_versions_select ON grounded.document_versions",
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

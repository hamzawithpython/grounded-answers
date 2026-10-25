"""Queue cleanup: a narrow definer that removes old finished jobs (ADR 0028, amends ADR 0015 decision 5).

Revision ID: 0015_queue_cleanup
Revises: 0014_purge
Create Date: 2026-10-25

Procrastinate's built-in remove_old_jobs (procrastinate/builtin_tasks.py, 3.10.0) needs DELETE on the whole job table,
and it runs a delete trigger that updates procrastinate_periodic_defers. Granting worker_user those would give it
rights over every queue row. So the cleanup is a definer, as the sweeps are (SECURITY.md, "Definer functions").

grounded.cleanup_finished_jobs(p_retention_hours) deletes jobs that are succeeded and whose last event is older than the
window. It removes nothing else: queued, running, retrying, failed, cancelled and aborted jobs stay, because failed jobs
are kept for diagnosis (ADR 0028). Deleting a job cascades to its events (procrastinate_events, ON DELETE CASCADE), and
the periodic-defer link is cleared first by the library's trigger.

The owner queue_cleanup_definer (NOLOGIN, created by bootstrap_definer_roles.sql) holds exactly the queue rights the
delete needs and the trigger uses: DELETE on the jobs, SELECT on the id, status, and event columns, and SELECT and UPDATE
of periodic_defers.job_id. worker_user gets EXECUTE on the function, and nothing else in the queue.
"""

from collections.abc import Sequence

from alembic import op

revision = "0015_queue_cleanup"
down_revision = "0014_purge"
branch_labels = None
depends_on = None

CLEANUP_FUNCTION = """
CREATE FUNCTION grounded.cleanup_finished_jobs(p_retention_hours integer) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, queue
AS $$
DECLARE
  removed bigint;
BEGIN
  IF p_retention_hours IS NULL OR p_retention_hours NOT BETWEEN 1 AND 8760 THEN
    RAISE EXCEPTION 'the retention window must be between 1 and 8760 hours' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  DELETE FROM queue.procrastinate_jobs AS j
  WHERE j.status = 'succeeded'
    AND (SELECT max(e.at) FROM queue.procrastinate_events AS e WHERE e.job_id = j.id)
        < pg_catalog.now() - pg_catalog.make_interval(hours => p_retention_hours);
  GET DIAGNOSTICS removed = ROW_COUNT;
  RETURN removed;
END;
$$
"""

UP: Sequence[str] = (
    # ---- the owner's queue rights: exactly what the delete and its trigger need ----
    "GRANT USAGE ON SCHEMA queue TO queue_cleanup_definer",
    "GRANT SELECT (id, status) ON queue.procrastinate_jobs TO queue_cleanup_definer",
    "GRANT DELETE ON queue.procrastinate_jobs TO queue_cleanup_definer",
    "GRANT SELECT (job_id, at) ON queue.procrastinate_events TO queue_cleanup_definer",
    "GRANT SELECT (job_id), UPDATE (job_id) ON queue.procrastinate_periodic_defers TO queue_cleanup_definer",
    CLEANUP_FUNCTION,
    "REVOKE ALL ON FUNCTION grounded.cleanup_finished_jobs(integer) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION grounded.cleanup_finished_jobs(integer) TO worker_user",
    # Owner handover, as in migration 0011: CREATE on the schema only for the change.
    "GRANT CREATE ON SCHEMA grounded TO queue_cleanup_definer",
    "ALTER FUNCTION grounded.cleanup_finished_jobs(integer) OWNER TO queue_cleanup_definer",
    "REVOKE CREATE ON SCHEMA grounded FROM queue_cleanup_definer",
)

DOWN: Sequence[str] = (
    "DROP FUNCTION grounded.cleanup_finished_jobs(integer)",
    "REVOKE SELECT (job_id), UPDATE (job_id) ON queue.procrastinate_periodic_defers FROM queue_cleanup_definer",
    "REVOKE SELECT (job_id, at) ON queue.procrastinate_events FROM queue_cleanup_definer",
    "REVOKE DELETE ON queue.procrastinate_jobs FROM queue_cleanup_definer",
    "REVOKE SELECT (id, status) ON queue.procrastinate_jobs FROM queue_cleanup_definer",
    "REVOKE USAGE ON SCHEMA queue FROM queue_cleanup_definer",
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

"""Let worker_user run Procrastinate's stalled-worker prune at startup (ADR 0015, ADR 0017).

Revision ID: 0006_worker_prune_grant
Revises: 0005_procrastinate_schema
Create Date: 2026-10-23

Every worker calls prune_stalled_workers when it starts (procrastinate/worker.py, run()). That
function is not SECURITY DEFINER, so worker_user needs EXECUTE on it. Its body is
DELETE FROM procrastinate_workers WHERE last_heartbeat < ... RETURNING id, so it also needs
SELECT on last_heartbeat. The id column already has SELECT from 0005. Without these grants the
worker fails at startup with "permission denied for function procrastinate_prune_stalled_workers_v1".

This is a new migration, not an edit of 0005, because 0005 is already merged (CLAUDE.md, migrations rule).
"""

from collections.abc import Sequence

from alembic import op

revision = "0006_worker_prune_grant"
down_revision = "0005_procrastinate_schema"
branch_labels = None
depends_on = None

UP: Sequence[str] = (
    "GRANT EXECUTE ON FUNCTION queue.procrastinate_prune_stalled_workers_v1(double precision) "
    "TO worker_user",
    "GRANT SELECT (last_heartbeat) ON queue.procrastinate_workers TO worker_user",
)

DOWN: Sequence[str] = (
    "REVOKE SELECT (last_heartbeat) ON queue.procrastinate_workers FROM worker_user",
    "REVOKE EXECUTE ON FUNCTION queue.procrastinate_prune_stalled_workers_v1(double precision) "
    "FROM worker_user",
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

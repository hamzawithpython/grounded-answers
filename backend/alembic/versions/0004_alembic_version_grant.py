"""Let app_user read the schema version, for the /ready schema check (ADR 0009).

Revision ID: 0004_alembic_version_grant
Revises: 0003_policies_and_definers
Create Date: 2026-10-22

The only grant on alembic_version. It is column-level: app_user reads version_num and nothing
else, and it cannot write the table. The table has no RLS (see RLS_EXEMPT_TABLES in
tests/db/test_catalog.py): the value is the migration name, which is not sensitive.

Runs as migration_role. This is a new migration, not an edit of 0002 or 0003, because those
are already applied to the dev database (CLAUDE.md, migrations rule).
"""

from collections.abc import Sequence

from alembic import op

revision = "0004_alembic_version_grant"
down_revision = "0003_policies_and_definers"
branch_labels = None
depends_on = None

UP: Sequence[str] = ("GRANT SELECT (version_num) ON grounded.alembic_version TO app_user",)

DOWN: Sequence[str] = ("REVOKE SELECT (version_num) ON grounded.alembic_version FROM app_user",)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

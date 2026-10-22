"""
Migration round-trip (ADR 0006): upgrade head, downgrade base, upgrade head.

The downgrade must undo every statement the upgrade ran (tables, constraints, trigger
function, column grants), and the second upgrade must rebuild the same schema. A bad
downgrade shows up here, not on a rollback in production.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config

from alembic import command

from .database import DbHandle

pytestmark = pytest.mark.db

BACKEND_DIR = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {
    "audit_log",
    "document_versions",
    "documents",
    "password_reset_tokens",
    "refresh_tokens",
    "tenant_memberships",
    "tenants",
    "users",
}


@contextmanager
def _migration_url(url: str) -> Iterator[None]:
    """Point the migration environment at url for the duration of one command."""
    previous = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = previous


def _tables(admin: psycopg.Connection) -> set[str]:
    rows = admin.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'grounded'").fetchall()
    return {name for (name,) in rows}


def test_upgrade_downgrade_upgrade_round_trip(grounded_db: DbHandle) -> None:
    """Head, then base, then head again: same tables, same head revision, nothing left over."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    migration_url = grounded_db.url_for(
        "migration_role", grounded_db.passwords["migration_role"]
    ).replace("postgresql://", "postgresql+psycopg://", 1)

    with _migration_url(migration_url):
        command.downgrade(cfg, "base")
        with psycopg.connect(grounded_db.admin_test_url()) as admin:
            assert _tables(admin) == {"alembic_version"}, "downgrade base left tables behind"
            leftovers = admin.execute(
                "SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'grounded'"
            ).fetchall()
            assert leftovers == [], f"downgrade base left functions behind: {leftovers}"

        command.upgrade(cfg, "head")

    with psycopg.connect(grounded_db.admin_test_url()) as admin:
        assert _tables(admin) == EXPECTED_TABLES | {"alembic_version"}
        head = admin.execute("SELECT version_num FROM grounded.alembic_version").fetchall()
        assert head == [("0002_roles_and_grants",)]
        triggers = admin.execute(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'grounded' AND NOT t.tgisinternal"
        ).fetchone()
        assert triggers == (3,), "updated_at triggers missing after the second upgrade"

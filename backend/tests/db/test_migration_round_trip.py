"""
Migration round-trip (ADR 0006): upgrade head, downgrade base, upgrade head.

It runs in its own temporary database, grounded_answers_test_roundtrip, which is built from
empty, bootstrapped with the same database SQL, and dropped afterwards. Because it never
touches the session database, its position in the run does not matter.

The downgrade must undo every statement the upgrade ran (tables, constraints, trigger function,
policies, functions, grants), and the second upgrade must rebuild the same schema.
"""

import os
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql

from alembic import command
from grounded_answers.readiness import EXPECTED_SCHEMA_HEAD

from .database import DbHandle
from .guard import ROUNDTRIP_DB_NAME, check_scratch_database, check_targets
from .psql import run_sql_file

pytestmark = pytest.mark.db

BACKEND_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = BACKEND_DIR / "scripts"

EXPECTED_TABLES = {
    "audit_log",
    "document_blobs",
    "document_versions",
    "documents",
    "password_reset_tokens",
    "refresh_tokens",
    "tenant_email_domains",
    "tenant_memberships",
    "tenants",
    "users",
}
EXPECTED_HEAD = EXPECTED_SCHEMA_HEAD


def _url_for_db(admin_url: str, dbname: str, user: str, password: str | None) -> str:
    """A URL for dbname as user. The scheme is the async driver that Alembic uses."""
    parts = urlsplit(admin_url)
    auth = user if password is None else f"{user}:{password}"
    netloc = f"{auth}@{parts.hostname}:{parts.port}"
    return urlunsplit(("postgresql+psycopg", netloc, f"/{dbname}", "", ""))


@contextmanager
def _migration_env(url: str) -> Iterator[None]:
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


@pytest.fixture
def roundtrip_db(grounded_db: DbHandle) -> Iterator[str]:
    """A fresh, bootstrapped, empty ROUNDTRIP database. Dropped at the end, even on failure."""
    check_targets(grounded_db.admin_url, [])
    check_scratch_database(ROUNDTRIP_DB_NAME)
    with psycopg.connect(grounded_db.admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(ROUNDTRIP_DB_NAME)
            )
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(ROUNDTRIP_DB_NAME)))
    try:
        psql = shlex.split(os.environ["PSQL_COMMAND"])
        run_sql_file(
            psql,
            SCRIPTS_DIR / "bootstrap_database.sql",
            ROUNDTRIP_DB_NAME,
            dict(os.environ),
            redact=[],
        )
        yield ROUNDTRIP_DB_NAME
    finally:
        with psycopg.connect(grounded_db.admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(ROUNDTRIP_DB_NAME)
                )
            )


def _tables(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'grounded'").fetchall()
    return {name for (name,) in rows}


def test_upgrade_downgrade_upgrade_round_trip(grounded_db: DbHandle, roundtrip_db: str) -> None:
    """Head, base, head again in an empty database: same schema, same head, nothing left over."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    migration_url = _url_for_db(
        grounded_db.admin_url,
        roundtrip_db,
        "migration_role",
        grounded_db.passwords["migration_role"],
    )
    admin_parts = urlsplit(grounded_db.admin_url)
    admin_url = urlunsplit((admin_parts.scheme, admin_parts.netloc, f"/{roundtrip_db}", "", ""))

    with _migration_env(migration_url):
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        with psycopg.connect(admin_url) as admin:
            assert _tables(admin) == {"alembic_version"}, "downgrade base left tables behind"
            functions = admin.execute(
                "SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'grounded'"
            ).fetchall()
            assert functions == [], f"downgrade base left functions behind: {functions}"
            policies = admin.execute(
                "SELECT count(*) FROM pg_policies WHERE schemaname = 'grounded'"
            ).fetchone()
            assert policies == (0,), "downgrade base left policies behind"
            # The queue schema is owned by the bootstrap and survives; its objects must not.
            queue_left = admin.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'queue' "
                "UNION ALL SELECT p.proname FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'queue' "
                "UNION ALL SELECT t.typname FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'queue' AND t.typtype IN ('e', 'c')"
            ).fetchall()
            assert queue_left == [], f"downgrade base left queue objects behind: {queue_left}"

        command.upgrade(cfg, "head")

    with psycopg.connect(admin_url) as admin:
        assert _tables(admin) == EXPECTED_TABLES | {"alembic_version"}
        head = admin.execute("SELECT version_num FROM grounded.alembic_version").fetchall()
        assert head == [(EXPECTED_HEAD,)]
        triggers = admin.execute(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'grounded' AND NOT t.tgisinternal"
        ).fetchone()
        assert triggers == (3,), "updated_at triggers missing after the second upgrade"

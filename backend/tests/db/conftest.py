"""
Session fixture that builds the test database (ADR 0006, amended 2026-10-22).

Order, once per pytest session:
  1. Guard: refuse unsafe targets before anything is dropped (guard.py).
  2. Drop and recreate grounded_answers_test (through the admin URL, which is the only
     connection with that privilege).
  3. Generate a fresh random password per login role for this session.
  4. Compute each role's SCRAM verifier with libpq (build_verifier), never by hand.
  5. Run the SAME SQL files the operator runs, through psql (PSQL_COMMAND):
       bootstrap_cluster.sql          against the maintenance database (login roles)
       bootstrap_definer_roles.sql    against the maintenance database (definer roles)
       bootstrap_database.sql         against grounded_answers_test

Failures raise. Nothing is skipped: a missing database or psql is an error (ADR 0006).
"""

import os
import secrets
import shlex
from collections.abc import Iterator
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql

from alembic import command
from grounded_answers.scram import build_verifier

from .auth_support import accounts, client  # noqa: F401 - fixtures for the auth HTTP tests
from .chunk_support import tenants  # noqa: F401 - fixture for the retrieval tests
from .database import LOGIN_ROLES, DbHandle
from .guard import TEST_DB_NAME, check_same_cluster, check_targets
from .ingest_support import admin, made  # noqa: F401 - fixtures for the ingestion tests
from .psql import run_sql_file, run_sql_query
from .seed import Seeded, seed

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
SCRIPTS_DIR = BACKEND_DIR / "scripts"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. DB tests fail when the database is unavailable; they never "
            "skip (ADR 0006). See docs/RUNBOOK.md, 'Running database tests'."
        )
    return value


def _dev_urls() -> list[str]:
    """Dev URLs from the environment and from backend/.env, for the guard's reuse check."""
    urls = [os.environ[k] for k in ("DATABASE_URL", "MIGRATION_DATABASE_URL") if k in os.environ]
    env_file = BACKEND_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in ("DATABASE_URL", "MIGRATION_DATABASE_URL"):
                urls.append(value.strip())
    return urls


# The cluster identity: the same value through the admin connection and through PSQL_COMMAND (ADR 0006).
SYSTEM_ID_SQL = "SELECT system_identifier::text FROM pg_control_system()"


@pytest.fixture(scope="session")
def grounded_db() -> Iterator[DbHandle]:
    """Fresh grounded_answers_test with cluster and database bootstrap applied."""
    admin_url = _required_env("TEST_ADMIN_DATABASE_URL")
    psql = shlex.split(_required_env("PSQL_COMMAND"))
    check_targets(admin_url, _dev_urls())

    passwords = {role: secrets.token_urlsafe(24) for role in LOGIN_ROLES}
    with psycopg.connect(admin_url) as bootstrap:
        verifiers = {role: build_verifier(bootstrap, role, pw) for role, pw in passwords.items()}

    env = dict(os.environ)
    for role, verifier in verifiers.items():
        env[f"{role.upper()}_SCRAM"] = verifier
    redact = list(verifiers.values())

    # Before anything is dropped or changed: PSQL_COMMAND must reach the cluster that the admin URL reaches.
    with psycopg.connect(admin_url) as identity:
        admin_system_id = identity.execute(SYSTEM_ID_SQL).fetchone()
    check_same_cluster(
        str(admin_system_id[0]) if admin_system_id else "",
        run_sql_query(psql, env, SYSTEM_ID_SQL),
    )

    with psycopg.connect(admin_url, autocommit=True) as bootstrap:
        bootstrap.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB_NAME))
        )
        bootstrap.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB_NAME)))

    run_sql_file(psql, SCRIPTS_DIR / "bootstrap_cluster.sql", "postgres", env, redact)
    run_sql_file(psql, SCRIPTS_DIR / "bootstrap_definer_roles.sql", "postgres", env, redact)
    run_sql_file(psql, SCRIPTS_DIR / "bootstrap_database.sql", TEST_DB_NAME, env, redact)

    # Schema: the migrations run as migration_role, the same role production uses (ADR 0005).
    admin_parts = urlsplit(admin_url)
    migration_url = urlunsplit(
        (
            "postgresql+psycopg",
            f"migration_role:{passwords['migration_role']}@"
            f"{admin_parts.hostname}:{admin_parts.port}",
            f"/{TEST_DB_NAME}",
            "",
            "",
        )
    )
    previous = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = migration_url
    try:
        command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = previous

    yield DbHandle(
        admin_url=admin_url,
        base=urlsplit(admin_url),
        passwords=MappingProxyType(passwords),
        verifiers=MappingProxyType(verifiers),
    )


@pytest.fixture(scope="session")
def seeded(grounded_db: DbHandle) -> Seeded:
    """Two complete tenants, written as the cluster admin (RLS bypass, test setup only)."""
    return seed(grounded_db.admin_test_url())

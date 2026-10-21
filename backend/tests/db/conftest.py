"""
Session fixture that builds the test database (ADR 0006, amended 2026-10-22).

Order, once per pytest session:
  1. Guard: refuse unsafe targets before anything is dropped (guard.py).
  2. Drop and recreate grounded_answers_test (through the admin URL, which is the only
     connection with that privilege).
  3. Generate a fresh random password per login role for this session.
  4. Compute each role's SCRAM verifier with libpq (build_verifier), never by hand.
  5. Run the SAME two SQL files the operator runs, through psql (PSQL_COMMAND):
       bootstrap_cluster.sql   against the maintenance database
       bootstrap_database.sql  against grounded_answers_test

Failures raise. Nothing is skipped: a missing database or psql is an error (ADR 0006).
"""

import os
import secrets
import shlex
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg import sql

from grounded_answers.scram import build_verifier

from .database import LOGIN_ROLES, DbHandle
from .guard import TEST_DB_NAME, check_targets

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


def _run_sql_file(
    psql: list[str],
    sql_file: Path,
    dbname: str,
    env: Mapping[str, str],
    redact: list[str],
) -> None:
    """Pipe one SQL file to psql. On failure, raise with stderr, redacting verifiers."""
    result = subprocess.run(  # noqa: S603 - PSQL_COMMAND is operator config from the env, not input (ADR 0006)
        [*psql, "-X", "-U", "postgres", "-d", dbname, "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql_file.read_bytes(),
        env=dict(env),
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")
        for value in redact:
            stderr = stderr.replace(value, "<redacted>")
        raise RuntimeError(
            f"{sql_file.name} failed in {dbname!r} (psql exit {result.returncode}):\n"
            f"{stderr[-2000:]}"
        )


@pytest.fixture(scope="session")
def grounded_db() -> Iterator[DbHandle]:
    """Fresh grounded_answers_test with cluster and database bootstrap applied."""
    admin_url = _required_env("TEST_ADMIN_DATABASE_URL")
    psql = shlex.split(_required_env("PSQL_COMMAND"))
    check_targets(admin_url, _dev_urls())

    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB_NAME))
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB_NAME)))

    passwords = {role: secrets.token_urlsafe(24) for role in LOGIN_ROLES}
    with psycopg.connect(admin_url) as admin:
        verifiers = {role: build_verifier(admin, role, pw) for role, pw in passwords.items()}

    env = dict(os.environ)
    for role, verifier in verifiers.items():
        env[f"{role.upper()}_SCRAM"] = verifier
    redact = list(verifiers.values())

    _run_sql_file(psql, SCRIPTS_DIR / "bootstrap_cluster.sql", "postgres", env, redact)
    _run_sql_file(psql, SCRIPTS_DIR / "bootstrap_database.sql", TEST_DB_NAME, env, redact)

    yield DbHandle(
        admin_url=admin_url,
        base=urlsplit(admin_url),
        passwords=MappingProxyType(passwords),
        verifiers=MappingProxyType(verifiers),
    )

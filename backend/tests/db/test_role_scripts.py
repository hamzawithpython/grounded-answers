"""
The two operator helpers from the RUNBOOK: single-role reset and login check (ADR 0006).

Both run against the test cluster. The reset writes back the verifier the fixture already set
for that role, so other tests keep their passwords.
"""

import os
import shlex
from pathlib import Path

import pytest

from grounded_answers.login_check import check_login

from .database import DbHandle
from .guard import TEST_DB_NAME
from .psql import run_sql_file

pytestmark = pytest.mark.db

RESET_SQL = Path(__file__).resolve().parents[2] / "scripts" / "reset_role_password.sql"


def _reset(grounded_db: DbHandle, role: str, verifier: str) -> None:
    env = dict(os.environ)
    env["ROLE_SCRAM"] = verifier
    run_sql_file(
        shlex.split(os.environ["PSQL_COMMAND"]),
        RESET_SQL,
        "postgres",
        env,
        [verifier],
        variables={"role": role},
    )


def test_reset_sets_a_password_that_logs_in(
    grounded_db: DbHandle, capsys: pytest.CaptureFixture[str]
) -> None:
    _reset(grounded_db, "app_user", grounded_db.verifiers["app_user"])
    ok = check_login(
        "app_user",
        grounded_db.passwords["app_user"],
        host=str(grounded_db.base.hostname),
        port=int(grounded_db.base.port or 5432),
        database=TEST_DB_NAME,
    )
    assert ok
    assert "LOGIN OK app_user" in capsys.readouterr().out


def test_reset_refuses_a_role_outside_the_login_allowlist(grounded_db: DbHandle) -> None:
    """The reset must never change postgres or a definer role's password."""
    with pytest.raises(RuntimeError, match="role must be migration_role, app_user or worker_user"):
        _reset(grounded_db, "postgres", grounded_db.verifiers["app_user"])


def test_login_check_reports_a_wrong_password(
    grounded_db: DbHandle, capsys: pytest.CaptureFixture[str]
) -> None:
    ok = check_login(
        "app_user",
        "wrong-password-that-is-not-set",
        host=str(grounded_db.base.hostname),
        port=int(grounded_db.base.port or 5432),
        database=TEST_DB_NAME,
    )
    assert not ok
    out = capsys.readouterr().out
    assert "LOGIN FAILED app_user" in out
    assert "wrong-password" not in out

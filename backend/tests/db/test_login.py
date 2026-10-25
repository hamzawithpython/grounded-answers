"""
Login tests (ADR 0006 amendment). Each role logs in with the password whose verifier was set
by the bootstrap SQL. This is a real SCRAM login over TCP, not SET ROLE.
"""

import psycopg
import pytest
from psycopg import OperationalError

from .database import LOGIN_ROLES, DbHandle

pytestmark = pytest.mark.db


@pytest.mark.parametrize("role", LOGIN_ROLES)
def test_role_logs_in_with_the_password_whose_verifier_was_set(
    grounded_db: DbHandle, role: str
) -> None:
    """The verifier loaded by bootstrap_cluster.sql authenticates the matching password."""
    url = grounded_db.url_for(role, grounded_db.passwords[role])
    with psycopg.connect(url) as conn:
        current = conn.execute("SELECT current_user").fetchone()
    assert current == (role,)


@pytest.mark.parametrize("role", LOGIN_ROLES)
def test_wrong_password_is_refused(grounded_db: DbHandle, role: str) -> None:
    """A different password for the same role fails SCRAM verification."""
    wrong = grounded_db.passwords[role] + "-wrong"
    with pytest.raises(OperationalError, match="password authentication failed"):
        psycopg.connect(grounded_db.url_for(role, wrong))


@pytest.mark.parametrize(
    "role", ["auth_definer", "retention_definer", "provisioning_definer", "retry_definer"]
)
def test_definer_roles_cannot_log_in(grounded_db: DbHandle, role: str) -> None:
    """
    Definer roles are NOLOGIN: they run functions but never hold a client session.

    The server may refuse on the missing password before it checks LOGIN, so the test
    accepts either refusal. What matters is that no connection succeeds.
    """
    with pytest.raises(
        OperationalError,
        match=r"not permitted to log in|no password supplied|password authentication failed",
    ):
        psycopg.connect(grounded_db.url_for(role))

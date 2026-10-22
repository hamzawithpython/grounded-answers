"""
updated_at trigger, exercised as app_user (ADR 0006 point 5, ARCHITECTURE section 3).

The trigger function is not SECURITY DEFINER and PUBLIC has no EXECUTE on it. This test proves
that the trigger still runs for app_user, which has no direct EXECUTE on the function, and that
app_user cannot write updated_at itself.
"""

from datetime import datetime

import psycopg
import pytest
from psycopg import errors

from .database import DbHandle
from .seed import Seeded

pytestmark = pytest.mark.db


def _updated_at(grounded_db: DbHandle, user: object) -> datetime:
    with psycopg.connect(grounded_db.admin_test_url()) as admin:
        row = admin.execute(
            "SELECT updated_at FROM grounded.users WHERE id = %s", (user,)
        ).fetchone()
    assert row is not None
    stamp: datetime = row[0]
    return stamp


def test_updated_at_moves_forward_on_update_as_app_user(
    grounded_db: DbHandle, seeded: Seeded
) -> None:
    """app_user changes display_name; the trigger sets updated_at to a later time."""
    before = _updated_at(grounded_db, seeded.a.employee)
    with psycopg.connect(
        grounded_db.url_for("app_user", grounded_db.passwords["app_user"])
    ) as conn:
        conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(seeded.a.tenant),))
        cur = conn.execute(
            "UPDATE grounded.users SET display_name = 'Renamed by test' WHERE id = %s",
            (seeded.a.employee,),
        )
        assert cur.rowcount == 1
        conn.commit()
    after = _updated_at(grounded_db, seeded.a.employee)
    assert after > before, f"updated_at did not advance: {before} -> {after}"


def test_app_user_cannot_set_updated_at_itself(grounded_db: DbHandle, seeded: Seeded) -> None:
    """No UPDATE grant on updated_at: the timestamp is the trigger's, not the app's."""
    with psycopg.connect(
        grounded_db.url_for("app_user", grounded_db.passwords["app_user"])
    ) as conn:
        conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(seeded.a.tenant),))
        with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
            conn.execute(
                "UPDATE grounded.users SET updated_at = '2000-01-01' WHERE id = %s",
                (seeded.a.employee,),
            )
        conn.rollback()

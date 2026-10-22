"""
create_tenant_user, the only path that creates employees (ADR 0004, decision B).

The actor and the tenant come from the session settings app.current_user and
app.current_tenant. They are never parameters. These tests set both in the same transaction,
the way the one auth code path will.

Every refusal must look the same from the caller's side: the same SQLSTATE and the same
primary message, whether the cause is a wrong domain, a duplicate email (in this tenant or
another), a non-admin actor, another tenant's admin, a missing tenant, or a missing actor.
Otherwise the function would tell callers which emails exist.
"""

import secrets
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import errors

from .database import DbHandle
from .seed import Seeded

pytestmark = pytest.mark.db

GENERIC = "could not create this user"


def _app(grounded_db: DbHandle) -> psycopg.Connection:
    return psycopg.connect(grounded_db.url_for("app_user", grounded_db.passwords["app_user"]))


def _admin(grounded_db: DbHandle) -> psycopg.Connection:
    return psycopg.connect(grounded_db.admin_test_url())


def _call(
    grounded_db: DbHandle,
    tenant: uuid.UUID | None,
    actor: uuid.UUID | None,
    email: str,
    role: str = "employee",
) -> uuid.UUID:
    """
    Call create_tenant_user as app_user, the way the app will: session settings first, then the
    call, all in one transaction. A None tenant or actor means that setting is not set.
    """
    with _app(grounded_db) as conn:
        if tenant is not None:
            conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))
        if actor is not None:
            conn.execute("SELECT set_config('app.current_user', %s, true)", (str(actor),))
        row = conn.execute(
            "SELECT grounded.create_tenant_user(%s, %s, %s)",
            (email, "New Person", role),
        ).fetchone()
        conn.commit()
    assert row is not None
    return uuid.UUID(str(row[0]))


def _membership_count(grounded_db: DbHandle, email: str) -> int:
    with _admin(grounded_db) as admin:
        row = admin.execute(
            "SELECT count(*) FROM grounded.users AS u JOIN grounded.tenant_memberships AS m "
            "ON m.user_id = u.id WHERE u.email = %s",
            (email,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _employee_email(grounded_db: DbHandle, user_id: uuid.UUID) -> str:
    with _admin(grounded_db) as admin:
        row = admin.execute("SELECT email FROM grounded.users WHERE id = %s", (user_id,)).fetchone()
    assert row is not None
    return str(row[0])


@pytest.fixture
def cleanup(grounded_db: DbHandle) -> Iterator[list[str]]:
    """Emails created by a test. Removed after it, so tests stay independent."""
    created: list[str] = []
    yield created
    with _admin(grounded_db) as admin:
        for email in created:
            admin.execute(
                "DELETE FROM grounded.tenant_memberships WHERE user_id IN "
                "(SELECT id FROM grounded.users WHERE email = %s)",
                (email,),
            )
            admin.execute("DELETE FROM grounded.users WHERE email = %s", (email,))
        admin.commit()


def test_admin_creates_an_employee_in_its_own_tenant(
    grounded_db: DbHandle, seeded: Seeded, cleanup: list[str]
) -> None:
    """Success: the user and the membership exist, with the role requested, in the session tenant."""
    email = f"new.{secrets.token_hex(4)}@{seeded.a.domain}"
    cleanup.append(email)
    new_user = _call(grounded_db, seeded.a.tenant, seeded.a.admin, email)

    with _admin(grounded_db) as admin:
        row = admin.execute(
            "SELECT m.tenant_id, m.role, m.active, u.active FROM grounded.users AS u "
            "JOIN grounded.tenant_memberships AS m ON m.user_id = u.id WHERE u.id = %s",
            (new_user,),
        ).fetchone()
    assert row == (seeded.a.tenant, "employee", True, True)


def _refusal(
    grounded_db: DbHandle, call: tuple[uuid.UUID | None, uuid.UUID | None, str]
) -> errors.RaiseException:
    tenant, actor, email = call
    with pytest.raises(errors.RaiseException) as excinfo:
        _call(grounded_db, tenant, actor, email)
    return excinfo.value


def test_every_refusal_looks_identical_to_the_caller(
    grounded_db: DbHandle, seeded: Seeded, cleanup: list[str]
) -> None:
    """
    Seven different causes. The same SQLSTATE and the same primary message for all of them.
    No row is created by any of them.
    """
    h = secrets.token_hex(4)
    fresh = f"fresh.{h}@{seeded.a.domain}"
    cleanup.append(fresh)
    existing_same_tenant = _employee_email(grounded_db, seeded.a.employee)
    existing_other_tenant = seeded.user_b_at_a_domain_email
    cases: dict[str, tuple[uuid.UUID | None, uuid.UUID | None, str]] = {
        "wrong domain": (seeded.a.tenant, seeded.a.admin, f"new.{h}@evil-{h}.test"),
        "duplicate, same tenant": (seeded.a.tenant, seeded.a.admin, existing_same_tenant),
        "duplicate, other tenant": (seeded.a.tenant, seeded.a.admin, existing_other_tenant),
        "non-admin actor": (seeded.a.tenant, seeded.a.employee, fresh),
        "other tenant's admin": (seeded.a.tenant, seeded.b.admin, fresh),
        "no tenant context": (None, seeded.a.admin, fresh),
        "no actor in session": (seeded.a.tenant, None, fresh),
    }
    before = {email: _membership_count(grounded_db, email) for _, _, email in cases.values()}

    seen: dict[str, tuple[str | None, str | None]] = {}
    for label, call in cases.items():
        exc = _refusal(grounded_db, call)
        seen[label] = (exc.sqlstate, exc.diag.message_primary)

    assert set(seen.values()) == {("P0001", GENERIC)}, f"refusals differ: {seen}"
    for _, _, email in cases.values():
        assert _membership_count(grounded_db, email) == before[email], f"row created for {email}"


def test_success_is_the_only_path_that_returns_a_user_id(
    grounded_db: DbHandle, seeded: Seeded, cleanup: list[str]
) -> None:
    """The one allowed case returns a uuid; a repeat of the same email does not."""
    email = f"once.{secrets.token_hex(4)}@{seeded.a.domain}"
    cleanup.append(email)
    assert isinstance(_call(grounded_db, seeded.a.tenant, seeded.a.admin, email), uuid.UUID)
    exc = _refusal(grounded_db, (seeded.a.tenant, seeded.a.admin, email))
    assert exc.diag.message_primary == GENERIC


def test_actor_cannot_be_passed_as_an_argument(grounded_db: DbHandle, seeded: Seeded) -> None:
    """The old three-argument-plus-actor signature is gone: the actor is never a parameter."""
    with _app(grounded_db) as conn:
        with pytest.raises(errors.UndefinedFunction):
            conn.execute(
                "SELECT grounded.create_tenant_user(%s, %s, %s, %s)",
                (seeded.a.admin, "x@example.test", "x", "employee"),
            )

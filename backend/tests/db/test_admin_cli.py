"""
Admin CLI against the real database, through the real logins (ADR 0004, ADR 0006).

The CLI connects as migration_role with the password the fixture generated. Results are checked
two ways: as the cluster admin (sees everything, used only to check the rows the CLI wrote),
and as app_user (the login the web app uses, to show what the app can and cannot see).
"""

import getpass
import secrets
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from argon2 import PasswordHasher
from click.testing import CliRunner, Result
from pytest import MonkeyPatch

from grounded_answers import admin_cli, admin_service
from tests.db.database import DbHandle

pytestmark = pytest.mark.db

GOOD_PASSWORD = secrets.token_urlsafe(16)


def _url(handle: DbHandle, role: str) -> str:
    """A psycopg-scheme URL for a login role on the test database."""
    password = handle.passwords[role]
    return handle.url_for(role, password).replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
def cli_as_migration_role(grounded_db: DbHandle, monkeypatch: MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MIGRATION_DATABASE_URL", _url(grounded_db, "migration_role"))
    yield


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(admin_cli.main, list(args))


def _slug() -> str:
    return f"t-{secrets.token_hex(4)}"


def _domain() -> str:
    return f"d{secrets.token_hex(4)}.test"


def _admin_rows(handle: DbHandle, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Read as the cluster admin. Test assertions only: the admin bypasses RLS."""
    with psycopg.connect(handle.admin_test_url()) as conn:
        return list(conn.execute(sql, params).fetchall())


def _tenant_id(handle: DbHandle, slug: str) -> Any:
    rows = _admin_rows(handle, "SELECT id FROM grounded.tenants WHERE slug = %s", (slug,))
    assert len(rows) == 1
    return rows[0][0]


def _make_tenant(handle: DbHandle) -> tuple[str, str]:
    """Create a tenant through the CLI. Returns (slug, domain), with the domain allowed."""
    slug, domain = _slug(), _domain()
    assert _invoke("create-tenant", "Acme Test", slug).exit_code == 0
    assert _invoke("add-domain", slug, domain).exit_code == 0
    return slug, domain


def _app_sees_tenant_slugs(handle: DbHandle, tenant_id: Any) -> list[str]:
    """What app_user sees in grounded.tenants with this tenant set (ADR 0005)."""
    with psycopg.connect(handle.url_for("app_user", handle.passwords["app_user"])) as conn:
        conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant_id),))
        return [row[0] for row in conn.execute("SELECT slug FROM grounded.tenants").fetchall()]


def test_create_tenant_writes_row_and_audit_entry(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    slug = _slug()
    result = _invoke("create-tenant", "Acme Test", slug)
    assert result.exit_code == 0, result.output
    assert f"Created tenant '{slug}'" in result.output

    tenant_id = _tenant_id(grounded_db, slug)
    rows = _admin_rows(
        grounded_db, "SELECT name, status FROM grounded.tenants WHERE slug = %s", (slug,)
    )
    assert rows == [("Acme Test", "active")]

    audit = _admin_rows(
        grounded_db,
        "SELECT actor_user_id, action, target_type, detail::text FROM grounded.audit_log "
        "WHERE tenant_id = %s AND action = 'tenant.create'",
        (tenant_id,),
    )
    assert audit == [(None, "tenant.create", "tenant", "{}")]


def test_app_user_sees_only_its_own_tenant_row(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    slug_a, _ = _make_tenant(grounded_db)
    slug_b, _ = _make_tenant(grounded_db)
    tenant_a = _tenant_id(grounded_db, slug_a)
    assert _app_sees_tenant_slugs(grounded_db, tenant_a) == [slug_a]
    assert slug_b not in _app_sees_tenant_slugs(grounded_db, tenant_a)


def test_duplicate_slug_is_refused_and_nothing_changes(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    slug = _slug()
    assert _invoke("create-tenant", "First", slug).exit_code == 0
    result = _invoke("create-tenant", "Second", slug)
    assert result.exit_code == 1
    assert f"Slug '{slug}' is already in use" in result.output
    count = _admin_rows(
        grounded_db, "SELECT count(*) FROM grounded.tenants WHERE slug = %s", (slug,)
    )
    assert count == [(1,)]


def test_domain_allowlist_add_list_remove(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    slug = _slug()
    assert _invoke("create-tenant", "Acme Test", slug).exit_code == 0
    assert "No domains are allowed" in _invoke("list-domains", slug).output

    assert _invoke("add-domain", slug, "acme.test").exit_code == 0
    assert _invoke("add-domain", slug, "ACME.test").exit_code == 1  # same domain, refused
    assert "already allowed" in _invoke("add-domain", slug, "acme.test").output
    assert _invoke("list-domains", slug).output.strip() == "acme.test"

    assert _invoke("remove-domain", slug, "acme.test").exit_code == 0
    again = _invoke("remove-domain", slug, "acme.test")
    assert again.exit_code == 1  # rowcount check: nothing was deleted, so this is a refusal
    assert "Nothing changed" in again.output


def test_domain_for_unknown_tenant_is_refused(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    result = _invoke("add-domain", _slug(), "acme.test")
    assert result.exit_code == 1
    assert "No tenant with slug" in result.output


def test_create_admin_writes_user_membership_and_audit(
    grounded_db: DbHandle, cli_as_migration_role: None, monkeypatch: MonkeyPatch
) -> None:
    slug, domain = _make_tenant(grounded_db)
    email = f"asha@{domain}"
    answers = iter([GOOD_PASSWORD, GOOD_PASSWORD])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))

    result = _invoke("create-admin", slug, email, "Asha Rao")
    assert result.exit_code == 0, result.output

    user = _admin_rows(
        grounded_db,
        "SELECT id, display_name, password_hash FROM grounded.users WHERE email = %s",
        (email,),
    )
    assert len(user) == 1
    user_id, display_name, stored_hash = user[0]
    assert display_name == "Asha Rao"
    assert stored_hash.startswith("$argon2id$")
    assert PasswordHasher().verify(stored_hash, GOOD_PASSWORD)  # the stored hash is the password's
    assert GOOD_PASSWORD not in stored_hash

    membership = _admin_rows(
        grounded_db,
        "SELECT tenant_id, role, active FROM grounded.tenant_memberships WHERE user_id = %s",
        (user_id,),
    )
    assert membership == [(_tenant_id(grounded_db, slug), "tenant_admin", True)]

    audit = _admin_rows(
        grounded_db,
        "SELECT actor_user_id, target_id, detail::text FROM grounded.audit_log "
        "WHERE action = 'user.create' AND target_id = %s",
        (user_id,),
    )
    assert audit == [(None, user_id, '{"role": "tenant_admin"}')]


def test_app_user_cannot_read_the_password_hash(
    grounded_db: DbHandle, cli_as_migration_role: None, monkeypatch: MonkeyPatch
) -> None:
    slug, domain = _make_tenant(grounded_db)
    email = f"ravi@{domain}"
    answers = iter([GOOD_PASSWORD, GOOD_PASSWORD])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert _invoke("create-admin", slug, email, "Ravi").exit_code == 0

    tenant_id = _tenant_id(grounded_db, slug)
    with psycopg.connect(
        grounded_db.url_for("app_user", grounded_db.passwords["app_user"])
    ) as conn:
        conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant_id),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT password_hash FROM grounded.users")


def test_create_admin_refused_for_a_domain_not_on_the_allowlist(
    grounded_db: DbHandle, cli_as_migration_role: None, monkeypatch: MonkeyPatch
) -> None:
    slug = _slug()
    assert _invoke("create-tenant", "Acme Test", slug).exit_code == 0
    email = f"sam@not-allowed-{secrets.token_hex(3)}.test"

    def _no_prompt(prompt: str = "") -> str:
        raise AssertionError("a wrong domain must be refused before the password prompt")

    monkeypatch.setattr(getpass, "getpass", _no_prompt)

    result = _invoke("create-admin", slug, email, "Sam")
    assert result.exit_code == 1
    assert "not on this tenant's allowlist" in result.output
    rows = _admin_rows(
        grounded_db, "SELECT count(*) FROM grounded.users WHERE email = %s", (email,)
    )
    assert rows == [(0,)]


def test_create_admin_refused_for_a_duplicate_email(
    grounded_db: DbHandle, cli_as_migration_role: None, monkeypatch: MonkeyPatch
) -> None:
    slug, domain = _make_tenant(grounded_db)
    email = f"dup@{domain}"
    answers = iter([GOOD_PASSWORD, GOOD_PASSWORD] * 2)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))

    assert _invoke("create-admin", slug, email, "First").exit_code == 0
    second = _invoke("create-admin", slug, email, "Second")
    assert second.exit_code == 1
    assert "already exists" in second.output
    rows = _admin_rows(
        grounded_db, "SELECT count(*) FROM grounded.users WHERE email = %s", (email,)
    )
    assert rows == [(1,)]


def test_failed_membership_insert_rolls_back_the_user(
    grounded_db: DbHandle, cli_as_migration_role: None, monkeypatch: MonkeyPatch
) -> None:
    """ADR 0004: a membership-less user must never be left behind."""
    slug, domain = _make_tenant(grounded_db)
    email = f"rollback@{domain}"
    answers = iter([GOOD_PASSWORD, GOOD_PASSWORD])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))

    async def _boom(session: object, **kwargs: object) -> None:
        raise RuntimeError("simulated membership failure")

    monkeypatch.setattr(admin_service, "_insert_membership", _boom)
    result = _invoke("create-admin", slug, email, "Rollback")
    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    rows = _admin_rows(
        grounded_db, "SELECT count(*) FROM grounded.users WHERE email = %s", (email,)
    )
    assert rows == [(0,)]


def test_cli_refuses_to_run_as_app_user(grounded_db: DbHandle, monkeypatch: MonkeyPatch) -> None:
    """A wrong URL must not make platform changes. The login must be migration_role."""
    monkeypatch.setenv("MIGRATION_DATABASE_URL", _url(grounded_db, "app_user"))
    slug = _slug()
    result = _invoke("create-tenant", "Should Not Exist", slug)
    assert result.exit_code == 1
    assert "must connect as migration_role" in result.output
    assert _admin_rows(
        grounded_db, "SELECT count(*) FROM grounded.tenants WHERE slug = %s", (slug,)
    ) == [(0,)]

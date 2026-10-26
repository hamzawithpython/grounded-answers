"""
The tenant-settings commands of the admin CLI, against the real database (ADR 0033, decisions 3 and 5).

Every change is audited with the old and new values of the fields that changed, and a refused or empty change writes
nothing. The policy is checked on the settings as they will be: a tenant cannot be moved to real data while a provider
that is not approved for real data is still listed.
"""

import json
import secrets
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from click.testing import CliRunner, Result
from pytest import MonkeyPatch

from grounded_answers import admin_cli
from tests.db.database import DbHandle

pytestmark = pytest.mark.db


@pytest.fixture
def cli_as_migration_role(grounded_db: DbHandle, monkeypatch: MonkeyPatch) -> Iterator[None]:
    password = grounded_db.passwords["migration_role"]
    url = grounded_db.url_for("migration_role", password).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
    yield


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(admin_cli.main, list(args))


def _rows(handle: DbHandle, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Read as the cluster admin. Test assertions only."""
    with psycopg.connect(handle.admin_test_url()) as conn:
        return list(conn.execute(sql, params).fetchall())


def _new_tenant(handle: DbHandle) -> str:
    slug = f"t-{secrets.token_hex(4)}"
    assert _invoke("create-tenant", "Settings Test", slug).exit_code == 0
    return slug


def _settings(handle: DbHandle, slug: str) -> tuple[Any, ...]:
    rows = _rows(
        handle,
        "SELECT s.answer_providers, s.data_class, s.daily_cost_cap_micro_usd "
        "FROM grounded.tenant_settings AS s JOIN grounded.tenants AS t ON t.id = s.tenant_id "
        "WHERE t.slug = %s",
        (slug,),
    )
    assert len(rows) == 1
    return rows[0]


def _audits(handle: DbHandle, slug: str) -> list[tuple[Any, ...]]:
    return _rows(
        handle,
        "SELECT a.action, a.actor_user_id, a.detail FROM grounded.audit_log AS a "
        "JOIN grounded.tenants AS t ON t.id = a.tenant_id "
        "WHERE t.slug = %s AND a.action = 'tenant_settings.update' ORDER BY a.created_at",
        (slug,),
    )


def test_create_tenant_gives_the_fail_closed_defaults(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    slug = _new_tenant(grounded_db)
    assert _settings(grounded_db, slug) == ([], "real", 0)
    shown = _invoke("show-tenant-settings", slug)
    assert shown.exit_code == 0
    assert "Providers: (none)" in shown.output
    assert "Data class: real" in shown.output
    assert "Daily cost cap: $0.000000" in shown.output


def test_set_settings_updates_the_row_and_audits_old_and_new(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    slug = _new_tenant(grounded_db)
    result = _invoke(
        "set-tenant-settings",
        slug,
        "--data-class",
        "fictional",
        "--providers",
        "groq,gemini",
        "--daily-cap-usd",
        "0.05",
    )
    assert result.exit_code == 0, result.output
    assert _settings(grounded_db, slug) == (["groq", "gemini"], "fictional", 50_000)

    (action, actor, detail) = _audits(grounded_db, slug)[0]
    assert action == "tenant_settings.update"
    assert actor is None  # the operator is not a user of any tenant
    assert detail == {
        "changes": {
            "answer_providers": {"old": [], "new": ["groq", "gemini"]},
            "data_class": {"old": "real", "new": "fictional"},
            "daily_cost_cap_micro_usd": {"old": 0, "new": 50_000},
        }
    }
    shown = _invoke("show-tenant-settings", slug)
    assert "Providers: groq, gemini" in shown.output
    assert "Daily cost cap: $0.050000" in shown.output


def test_a_partial_change_keeps_the_other_fields_and_audits_only_what_changed(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    slug = _new_tenant(grounded_db)
    assert (
        _invoke(
            "set-tenant-settings",
            slug,
            "--data-class",
            "fictional",
            "--providers",
            "groq",
            "--daily-cap-usd",
            "0.05",
        ).exit_code
        == 0
    )
    assert _invoke("set-tenant-settings", slug, "--daily-cap-usd", "0.10").exit_code == 0
    assert _settings(grounded_db, slug) == (["groq"], "fictional", 100_000)
    audits = _audits(grounded_db, slug)
    assert len(audits) == 2
    assert audits[1][2] == {
        "changes": {"daily_cost_cap_micro_usd": {"old": 50_000, "new": 100_000}}
    }


def test_a_change_that_changes_nothing_writes_no_audit_row(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    slug = _new_tenant(grounded_db)
    args = ("set-tenant-settings", slug, "--data-class", "fictional", "--providers", "groq")
    assert _invoke(*args).exit_code == 0
    before = len(_audits(grounded_db, slug))
    again = _invoke(*args)
    assert again.exit_code == 0
    assert "No change" in again.output
    assert len(_audits(grounded_db, slug)) == before == 1


def test_real_data_refuses_providers_that_are_not_approved(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    slug = _new_tenant(grounded_db)
    result = _invoke("set-tenant-settings", slug, "--providers", "groq")  # class is still 'real'
    assert result.exit_code != 0
    assert "Not approved for real data: groq" in result.output
    assert "Nothing changed" in result.output
    assert _settings(grounded_db, slug) == ([], "real", 0)
    assert _audits(grounded_db, slug) == []


def test_moving_to_real_data_is_refused_while_a_free_tier_provider_is_listed(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    slug = _new_tenant(grounded_db)
    assert (
        _invoke(
            "set-tenant-settings", slug, "--data-class", "fictional", "--providers", "groq"
        ).exit_code
        == 0
    )
    refused = _invoke("set-tenant-settings", slug, "--data-class", "real")
    assert refused.exit_code != 0
    assert _settings(grounded_db, slug) == (["groq"], "fictional", 0)
    assert len(_audits(grounded_db, slug)) == 1  # only the first change

    allowed = _invoke(
        "set-tenant-settings", slug, "--data-class", "real", "--providers", "anthropic"
    )
    assert allowed.exit_code == 0, allowed.output
    assert _settings(grounded_db, slug) == (["anthropic"], "real", 0)
    last = _audits(grounded_db, slug)[-1][2]
    assert last == {
        "changes": {
            "answer_providers": {"old": ["groq"], "new": ["anthropic"]},
            "data_class": {"old": "fictional", "new": "real"},
        }
    }


@pytest.mark.parametrize(
    "args",
    [
        ("--providers", "openai", "--data-class", "fictional"),
        ("--providers", "groq,groq", "--data-class", "fictional"),
        ("--data-class", "public"),
        ("--daily-cap-usd", "abc"),
        ("--daily-cap-usd", "-1"),
        ("--daily-cap-usd", "1000.01"),
        ("--daily-cap-usd", "0.0000001"),
    ],
)
def test_bad_values_are_refused_and_change_nothing(
    grounded_db: DbHandle, cli_as_migration_role: None, args: tuple[str, ...]
) -> None:
    slug = _new_tenant(grounded_db)
    result = _invoke("set-tenant-settings", slug, *args)
    assert result.exit_code != 0
    assert _settings(grounded_db, slug) == ([], "real", 0)
    assert _audits(grounded_db, slug) == []


def test_an_empty_provider_list_clears_the_providers(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    slug = _new_tenant(grounded_db)
    assert (
        _invoke(
            "set-tenant-settings", slug, "--data-class", "fictional", "--providers", "groq"
        ).exit_code
        == 0
    )
    assert _invoke("set-tenant-settings", slug, "--providers", "").exit_code == 0
    assert _settings(grounded_db, slug) == ([], "fictional", 0)


def test_an_unknown_tenant_is_refused(grounded_db: DbHandle, cli_as_migration_role: None) -> None:
    result = _invoke("set-tenant-settings", "no-such-tenant", "--daily-cap-usd", "1")
    assert result.exit_code != 0
    assert "No tenant with slug" in result.output
    assert _invoke("show-tenant-settings", "no-such-tenant").exit_code != 0


def test_a_tenant_without_a_row_is_shown_with_defaults_and_gets_a_row_on_change(
    grounded_db: DbHandle, cli_as_migration_role: None
) -> None:
    """A tenant inserted without the CLI (as the test seed does) has no row. The CLI treats it as the defaults."""
    slug = _new_tenant(grounded_db)
    with psycopg.connect(grounded_db.admin_test_url()) as conn:
        conn.execute(
            "DELETE FROM grounded.tenant_settings WHERE tenant_id = "
            "(SELECT id FROM grounded.tenants WHERE slug = %s)",
            (slug,),
        )
    assert "Providers: (none)" in _invoke("show-tenant-settings", slug).output
    assert _invoke("set-tenant-settings", slug, "--daily-cap-usd", "0.05").exit_code == 0
    assert _settings(grounded_db, slug) == ([], "real", 50_000)


def test_the_audit_detail_holds_no_secret_or_text(
    grounded_db: DbHandle, cli_as_migration_role: None, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + secrets.token_hex(16))
    slug = _new_tenant(grounded_db)
    assert (
        _invoke(
            "set-tenant-settings",
            slug,
            "--data-class",
            "fictional",
            "--providers",
            "groq",
            "--daily-cap-usd",
            "0.05",
        ).exit_code
        == 0
    )
    text = json.dumps([row[2] for row in _audits(grounded_db, slug)])
    assert "gsk_" not in text
    assert "key" not in text.lower()

"""
Admin CLI refusals and prompts, without a database (ADR 0004, ADR 0010).

Every refusal here must happen before any connection is made. The tests replace the
database runner with one that fails, so a refusal that reaches the database fails the test.
"""

import asyncio
import getpass
import secrets
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import pytest
from click.testing import CliRunner
from pytest import MonkeyPatch

from grounded_answers import admin_cli, admin_service

VALID_URL = "postgresql+psycopg://migration_role@127.0.0.1:5432/grounded_answers"
# With a password, so the CLI never prompts for the database password in these tests.
VALID_URL_WITH_PASSWORD = f"postgresql+psycopg://migration_role:{secrets.token_urlsafe(8)}@127.0.0.1:5432/grounded_answers"
GOOD_PASSWORD = secrets.token_urlsafe(16)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def no_database(monkeypatch: MonkeyPatch) -> None:
    """Any attempt to open a database transaction fails the test."""

    def _fail(action: Callable[..., Awaitable[Any]]) -> Any:
        raise AssertionError("refusal should have happened before the database")

    monkeypatch.setattr(admin_cli, "_run", _fail)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", VALID_URL)


@pytest.fixture
def precheck_passes(monkeypatch: MonkeyPatch) -> None:
    """The tenant and domain pre-check succeeds without a database. Later steps are not run."""

    async def _ok(session: object, **kwargs: Any) -> str:
        return str(kwargs["email"])

    monkeypatch.setattr(admin_service, "check_admin_target", _ok)
    monkeypatch.setattr(admin_cli, "_execute", lambda url, action: action(object()))
    monkeypatch.setenv("MIGRATION_DATABASE_URL", VALID_URL_WITH_PASSWORD)


def test_help_lists_every_command(runner: CliRunner) -> None:
    result = runner.invoke(admin_cli.main, ["--help"])
    assert result.exit_code == 0
    for command in ("create-tenant", "add-domain", "remove-domain", "list-domains", "create-admin"):
        assert command in result.output


def test_missing_migration_url_is_refused(runner: CliRunner, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    result = runner.invoke(admin_cli.main, ["list-domains", "acme"])
    assert result.exit_code == 1
    assert "MIGRATION_DATABASE_URL is not set" in result.output
    assert "Traceback" not in result.output


def test_invalid_url_message_does_not_echo_the_url(
    runner: CliRunner, monkeypatch: MonkeyPatch
) -> None:
    garbage = "not-a-url-with-marker-123"
    monkeypatch.setenv("MIGRATION_DATABASE_URL", garbage)
    result = runner.invoke(admin_cli.main, ["list-domains", "acme"])
    assert result.exit_code == 1
    assert "not a valid URL" in result.output
    assert garbage not in result.output


def test_url_with_password_is_used_without_prompting(monkeypatch: MonkeyPatch) -> None:
    def _no_prompt(prompt: str = "") -> str:
        raise AssertionError("the password is already in the URL; do not prompt")

    monkeypatch.setattr(getpass, "getpass", _no_prompt)
    in_url = secrets.token_urlsafe(8)
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL", f"postgresql+psycopg://migration_role:{in_url}@h/db"
    )
    url = admin_cli._migration_url()
    assert url.password == in_url


def test_url_without_password_prompts_for_it(monkeypatch: MonkeyPatch) -> None:
    prompts: list[str] = []
    typed = secrets.token_urlsafe(8)

    def _prompt(prompt: str = "") -> str:
        prompts.append(prompt)
        return typed

    monkeypatch.setattr(getpass, "getpass", _prompt)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", VALID_URL)
    url = admin_cli._migration_url()
    assert url.password == typed
    assert prompts == ["migration_role password: "]


def test_invalid_slug_is_refused_before_the_database(runner: CliRunner, no_database: None) -> None:
    result = runner.invoke(admin_cli.main, ["create-tenant", "Acme", "Not_A_Slug"])
    assert result.exit_code == 1
    assert "Slug must be" in result.output


def test_invalid_domain_is_refused_before_the_database(
    runner: CliRunner, no_database: None
) -> None:
    result = runner.invoke(admin_cli.main, ["add-domain", "acme", "nodot"])
    assert result.exit_code == 1
    assert "Domain must look like" in result.output


def test_invalid_email_is_refused_before_the_database(
    runner: CliRunner, monkeypatch: MonkeyPatch, no_database: None
) -> None:
    def _no_prompt(prompt: str = "") -> str:
        raise AssertionError("bad input must fail before the password prompt")

    monkeypatch.setattr(getpass, "getpass", _no_prompt)
    result = runner.invoke(admin_cli.main, ["create-admin", "acme", "not-an-email", "Asha"])
    assert result.exit_code == 1
    assert "Email must look like" in result.output


def test_password_mismatch_is_refused_before_the_database(
    runner: CliRunner, monkeypatch: MonkeyPatch, precheck_passes: None
) -> None:
    answers = iter(["correct-horse-battery", "correct-horse-batterY"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    result = runner.invoke(admin_cli.main, ["create-admin", "acme", "a@acme.test", "Asha"])
    assert result.exit_code == 1
    assert "do not match" in result.output


def test_common_password_is_refused_before_the_database(
    runner: CliRunner, monkeypatch: MonkeyPatch, precheck_passes: None
) -> None:
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "unbelievable")
    result = runner.invoke(admin_cli.main, ["create-admin", "acme", "a@acme.test", "Asha"])
    assert result.exit_code == 1
    assert "common-password list" in result.output


def test_create_admin_passes_the_checked_password_to_the_service(
    runner: CliRunner, monkeypatch: MonkeyPatch
) -> None:
    """Both prompts are asked; the service receives the normalised email and the password."""
    answers = iter([GOOD_PASSWORD, GOOD_PASSWORD])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setenv("MIGRATION_DATABASE_URL", VALID_URL)
    captured: dict[str, Any] = {}
    new_user = uuid.uuid4()

    async def _fake_service(session: object, **kwargs: Any) -> uuid.UUID:
        captured.update(kwargs)
        return new_user

    def _run_without_database(action: Callable[[Any], Coroutine[Any, Any, Any]]) -> Any:
        return asyncio.run(action(object()))

    async def _precheck_ok(session: object, **kwargs: Any) -> str:
        return str(kwargs["email"])

    monkeypatch.setattr(admin_cli, "_run", _run_without_database)
    monkeypatch.setattr(admin_service, "check_admin_target", _precheck_ok)
    monkeypatch.setattr(admin_service, "create_tenant_admin", _fake_service)

    result = runner.invoke(
        admin_cli.main, ["create-admin", "acme", "  Asha@Acme.TEST ", "  Asha Rao  "]
    )
    assert result.exit_code == 0, result.output
    assert captured == {
        "tenant_slug": "acme",
        "email": "asha@acme.test",
        "display_name": "Asha Rao",
        "password": GOOD_PASSWORD,
    }
    assert str(new_user) in result.output
    assert GOOD_PASSWORD not in result.output


def test_wrong_domain_is_refused_before_the_password_prompt(
    runner: CliRunner, monkeypatch: MonkeyPatch
) -> None:
    """The tenant and domain are checked first, so a wrong domain never asks for a password."""

    def _no_prompt(prompt: str = "") -> str:
        raise AssertionError("the domain check must fail before any password prompt")

    checked: list[str] = []

    async def _refuse(session: object, **kwargs: Any) -> str:
        checked.append(str(kwargs["email"]))
        raise admin_service.AdminError("'elsewhere.test' is not on this tenant's allowlist.")

    monkeypatch.setattr(getpass, "getpass", _no_prompt)
    monkeypatch.setattr(admin_service, "check_admin_target", _refuse)
    monkeypatch.setattr(admin_cli, "_execute", lambda url, action: action(object()))
    monkeypatch.setenv("MIGRATION_DATABASE_URL", VALID_URL_WITH_PASSWORD)

    result = runner.invoke(admin_cli.main, ["create-admin", "acme", "sam@elsewhere.test", "Sam"])
    assert result.exit_code == 1
    assert checked == ["sam@elsewhere.test"]
    assert "not on this tenant's allowlist" in result.output

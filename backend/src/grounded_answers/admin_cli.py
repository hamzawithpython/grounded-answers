"""
Platform-owner admin CLI (ADR 0004, ADR 0010).

Run from the backend directory, in a shell where MIGRATION_DATABASE_URL is set:

    uv run --group operator python -m grounded_answers.admin_cli --help

Credentials (ADR 0004): the migration role's URL comes from the process environment only.
This module never reads backend/.env, so Settings never sees it. If the URL has no password,
the CLI asks for one with a hidden prompt. Passwords are never command-line arguments, so they
do not reach shell history or the process list.

Each command runs in one transaction on the event loop from grounded_answers.runtime
(psycopg needs a selector loop on Windows, ADR 0005). Refusals print a short message and exit
non-zero. Driver messages are not printed, because they can contain the host (SECURITY.md).
"""

import asyncio
import getpass
import os
import uuid
from collections.abc import Awaitable, Callable

import click
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from grounded_answers import admin_service
from grounded_answers.admin_service import AdminError
from grounded_answers.database import create_database_engine
from grounded_answers.passwords import PasswordPolicyError, check_password_policy
from grounded_answers.rate_limit import unlock_account, wall_clock_ms
from grounded_answers.runtime import selector_loop_factory

EXPECTED_ROLE = "migration_role"


def _migration_url() -> URL:
    """Read MIGRATION_DATABASE_URL from the shell. Ask for the password if the URL has none."""
    raw = os.environ.get("MIGRATION_DATABASE_URL")
    if not raw:
        raise click.ClickException(
            "MIGRATION_DATABASE_URL is not set. Set it in this shell only (docs/RUNBOOK.md, "
            "Admin CLI)."
        )
    try:
        url = make_url(raw)
    except ArgumentError as exc:
        # The parser's message can echo part of the URL, so it is not shown.
        raise click.ClickException("MIGRATION_DATABASE_URL is not a valid URL.") from exc
    if url.password is None:
        return url.set(password=getpass.getpass("migration_role password: "))
    return url


def _new_password() -> str:
    """Ask twice, check the match, then check the policy (ADR 0004). Both prompts are hidden."""
    first = getpass.getpass("Password for the first tenant admin: ")
    second = getpass.getpass("Repeat the password: ")
    if first != second:
        raise click.ClickException("The two passwords do not match. Nothing was changed.")
    try:
        check_password_policy(first)
    except PasswordPolicyError as exc:
        raise click.ClickException(str(exc)) from exc
    return first


async def _assert_migration_role(session: AsyncSession) -> None:
    """Refuse to run as any other login, so a wrong URL cannot make platform changes."""
    result = await session.execute(text("SELECT current_user"))
    if result.scalar_one() != EXPECTED_ROLE:
        raise AdminError("The CLI must connect as migration_role. Nothing was changed.")


async def _execute[T](url: URL, action: Callable[[AsyncSession], Awaitable[T]]) -> T:
    engine = create_database_engine(url.render_as_string(hide_password=False))
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
            await _assert_migration_role(session)
            return await action(session)
    finally:
        await engine.dispose()


def _run[T](action: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Open the migration connection, run one transaction, and turn errors into exit codes."""
    url = _migration_url()
    try:
        with asyncio.Runner(loop_factory=selector_loop_factory) as runner:
            return runner.run(_execute(url, action))
    except AdminError as exc:
        raise click.ClickException(str(exc)) from exc
    except SQLAlchemyError as exc:
        # Only the exception class is shown: driver text can contain the host.
        raise click.ClickException(
            f"Database error ({type(exc).__name__}). Nothing was changed."
        ) from exc


def _validated(check: Callable[[str], str], raw: str) -> str:
    """Run a normalize_* check and turn its refusal into a clean CLI error."""
    try:
        return check(raw)
    except AdminError as exc:
        raise click.ClickException(str(exc)) from exc


def _validated_int(check: Callable[[str], int], raw: str) -> int:
    """Like _validated, for a check that returns a number."""
    try:
        return check(raw)
    except AdminError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def main() -> None:
    """Platform-owner commands. They use MIGRATION_DATABASE_URL (migration_role, ADR 0004)."""


@main.command("create-tenant")
@click.argument("name")
@click.argument("slug")
def create_tenant_command(name: str, slug: str) -> None:
    """Create a tenant. SLUG is lowercase letters, digits and hyphens (3 to 40 characters)."""
    clean_slug = _validated(admin_service.normalize_slug, slug)
    tenant_id = _run(lambda s: admin_service.create_tenant(s, name=name, slug=clean_slug))
    click.echo(f"Created tenant '{clean_slug}' ({tenant_id}).")


@main.command("add-domain")
@click.argument("tenant_slug")
@click.argument("domain")
def add_domain_command(tenant_slug: str, domain: str) -> None:
    """Allow accounts at DOMAIN (for example example.com) for TENANT_SLUG."""
    clean_domain = _validated(admin_service.normalize_domain, domain)
    _run(lambda s: admin_service.add_domain(s, tenant_slug=tenant_slug, domain=clean_domain))
    click.echo(f"Allowed '{clean_domain}' for tenant '{tenant_slug}'.")


@main.command("remove-domain")
@click.argument("tenant_slug")
@click.argument("domain")
def remove_domain_command(tenant_slug: str, domain: str) -> None:
    """Remove DOMAIN from the allowlist for TENANT_SLUG. Existing accounts are not changed."""
    clean_domain = _validated(admin_service.normalize_domain, domain)
    _run(lambda s: admin_service.remove_domain(s, tenant_slug=tenant_slug, domain=clean_domain))
    click.echo(f"Removed '{clean_domain}' from tenant '{tenant_slug}'.")


@main.command("list-domains")
@click.argument("tenant_slug")
def list_domains_command(tenant_slug: str) -> None:
    """List the allowed domains for TENANT_SLUG."""
    domains = _run(lambda s: admin_service.list_domains(s, tenant_slug=tenant_slug))
    if not domains:
        click.echo(f"No domains are allowed for tenant '{tenant_slug}'.")
        return
    for domain in domains:
        click.echo(domain)


@main.command("create-admin")
@click.argument("tenant_slug")
@click.argument("email")
@click.argument("display_name")
def create_admin_command(tenant_slug: str, email: str, display_name: str) -> None:
    """Create the first tenant_admin for TENANT_SLUG. The password is asked for twice."""
    clean_email = _validated(admin_service.normalize_email, email)
    clean_name = _validated(admin_service.normalize_display_name, display_name)
    # Fail fast: check the tenant and the domain before asking for a password (no writes).
    _run(lambda s: admin_service.check_admin_target(s, tenant_slug=tenant_slug, email=clean_email))
    password = _new_password()
    user_id: uuid.UUID = _run(
        lambda s: admin_service.create_tenant_admin(
            s,
            tenant_slug=tenant_slug,
            email=clean_email,
            display_name=clean_name,
            password=password,
        )
    )
    click.echo(f"Created tenant admin ({user_id}) for tenant '{tenant_slug}'.")


@main.command("show-tenant-settings")
@click.argument("tenant_slug")
def show_tenant_settings_command(tenant_slug: str) -> None:
    """Show the answer providers, data class and daily cost cap of TENANT_SLUG."""
    view = _run(lambda s: admin_service.show_tenant_settings(s, tenant_slug=tenant_slug))
    click.echo(f"Providers: {', '.join(view.answer_providers) or '(none)'}")
    click.echo(f"Data class: {view.data_class}")
    click.echo(f"Daily cost cap: ${admin_service.micro_to_usd(view.daily_cost_cap_micro_usd)}")


@main.command("set-tenant-settings")
@click.argument("tenant_slug")
@click.option(
    "--providers",
    default=None,
    help="Comma-separated, in order of preference: groq, gemini, anthropic. Empty string = none.",
)
@click.option("--data-class", default=None, help="fictional or real.")
@click.option(
    "--daily-cap-usd", default=None, help="Daily cost cap in US dollars, for example 0.05."
)
def set_tenant_settings_command(
    tenant_slug: str, providers: str | None, data_class: str | None, daily_cap_usd: str | None
) -> None:
    """
    Change the answer settings of TENANT_SLUG (ADR 0033). Options you leave out stay as they are.

    A tenant with real data may use only providers approved for real data. The change is audited.
    """
    provider_list = (
        None
        if providers is None
        else [name.strip() for name in providers.split(",") if name.strip()]
    )
    cap = (
        None if daily_cap_usd is None else _validated_int(admin_service.usd_to_micro, daily_cap_usd)
    )
    changes = _run(
        lambda s: admin_service.update_tenant_settings(
            s,
            tenant_slug=tenant_slug,
            providers=provider_list,
            data_class=data_class,
            daily_cap_micro_usd=cap,
        )
    )
    if not changes:
        click.echo(f"No change for tenant '{tenant_slug}'.")
        return
    click.echo(f"Updated tenant '{tenant_slug}': {', '.join(sorted(changes))}.")


@main.command("unlock-account")
@click.argument("tenant_slug")
@click.argument("email")
def unlock_account_command(tenant_slug: str, email: str) -> None:
    """
    Clear the hard lock and the current-hour counters for EMAIL (ADR 0014).

    Needs REDIS_URL and AUTH_RATE_LIMIT_HMAC_KEY in this shell, as the migration URL is. The CLI never
    reads backend/.env. Valkey keys are removed first; if the audit row then fails, the command says so.
    """
    clean_email = _validated(admin_service.normalize_email, email)
    rate_key = _shell_secret("AUTH_RATE_LIMIT_HMAC_KEY")
    removed = _remove_lock_keys(rate_key, clean_email)
    _run(
        lambda s: admin_service.record_account_unlock(
            s, tenant_slug=tenant_slug, keys_removed=removed
        )
    )
    click.echo(f"Unlocked the account in tenant '{tenant_slug}'. Keys removed: {removed}.")


def _shell_secret(name: str) -> bytes:
    value = os.environ.get(name, "")
    if len(value) < 32:
        raise click.ClickException(f"{name} must be set in this shell, at least 32 characters.")
    return value.encode("ascii")


def _remove_lock_keys(rate_key: bytes, email: str) -> int:
    """Delete the account's lock keys in Valkey. Errors name the problem, never the URL."""
    url = os.environ.get("REDIS_URL")
    if not url:
        raise click.ClickException("REDIS_URL must be set in this shell.")

    async def _go() -> int:
        client = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        try:
            return await unlock_account(client, rate_key, email, wall_clock_ms())
        finally:
            await client.aclose()

    try:
        return asyncio.run(_go())
    except RedisError as exc:
        raise click.ClickException(
            f"Valkey unavailable ({type(exc).__name__}). Nothing was unlocked."
        ) from exc


if __name__ == "__main__":
    main()

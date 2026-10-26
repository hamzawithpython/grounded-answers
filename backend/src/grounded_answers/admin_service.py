"""
Platform-owner operations: tenants, the email-domain allowlist, and first tenant admins (ADR 0004).

These functions run inside one transaction that the caller opens (see admin_cli.py). They use
the migration role, which is the only role with write access to tenants and the allowlist
(ADR 0004, ADR 0003 policies). The web app never calls this module.

SQL is written with text() and bind parameters. ORM models arrive in Session 3, Step 2
(ADR 0005); until then the explicit SQL here is the same SQL the migrations define.

Every write also adds an audit_log row with actor_user_id = NULL: the operator is not a user
of any tenant. The detail holds counts or the domain only, never a password or a personal
email (ADR 0007 rule for audit detail).
"""

import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from grounded_answers.answering.policy import (
    ProviderPolicyError,
    check_data_class,
    check_providers,
)
from grounded_answers.passwords import PasswordPolicyError, check_password_policy, hash_password

SLUG_RE = re.compile(r"^[a-z0-9-]{3,40}$")
DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[a-z0-9.-]{3,253}$")


class AdminError(Exception):
    """A refusal the operator can act on. The message names the problem, never a secret."""


def normalize_slug(raw: str) -> str:
    slug = raw.strip().lower()
    if not SLUG_RE.fullmatch(slug):
        raise AdminError("Slug must be 3 to 40 characters: lowercase letters, digits, hyphens.")
    return slug


def normalize_domain(raw: str) -> str:
    domain = raw.strip().lower()
    if not (4 <= len(domain) <= 253 and DOMAIN_RE.fullmatch(domain)):
        raise AdminError("Domain must look like example.com (lowercase, with a dot).")
    return domain


def normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise AdminError("Email must look like name@example.com.")
    try:
        normalize_domain(email.split("@", 1)[1])
    except AdminError as exc:
        raise AdminError("Email domain is not valid.") from exc
    return email


def normalize_display_name(raw: str) -> str:
    name = raw.strip()
    if not 1 <= len(name) <= 120:
        raise AdminError("Display name must be 1 to 120 characters.")
    return name


def _is_unique_violation(exc: IntegrityError) -> bool:
    return isinstance(exc.orig, psycopg.errors.UniqueViolation)


async def _tenant_id_for_slug(session: AsyncSession, slug: str) -> uuid.UUID:
    result = await session.execute(
        text("SELECT id FROM grounded.tenants WHERE slug = :slug"), {"slug": slug}
    )
    row = result.first()
    if row is None:
        raise AdminError(f"No tenant with slug '{slug}'.")
    tenant_id: uuid.UUID = row.id
    return tenant_id


async def _audit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    action: str,
    target_type: str,
    target_id: uuid.UUID | None,
    detail: dict[str, Any],
) -> None:
    await session.execute(
        text(
            "INSERT INTO grounded.audit_log "
            "(id, tenant_id, actor_user_id, action, target_type, target_id, detail) "
            "VALUES (:id, :tenant_id, NULL, :action, :target_type, :target_id, "
            "CAST(:detail AS jsonb))"
        ),
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "detail": json.dumps(detail),
        },
    )


async def create_tenant(session: AsyncSession, *, name: str, slug: str) -> uuid.UUID:
    """Create an active tenant. The slug must be unique."""
    clean_name = name.strip()
    if not 1 <= len(clean_name) <= 200:
        raise AdminError("Tenant name must be 1 to 200 characters.")
    clean_slug = normalize_slug(slug)
    tenant_id = uuid.uuid4()
    try:
        await session.execute(
            text("INSERT INTO grounded.tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": clean_name, "slug": clean_slug},
        )
    except IntegrityError as exc:
        if _is_unique_violation(exc):
            raise AdminError(f"Slug '{clean_slug}' is already in use.") from exc
        raise
    # The fail-closed defaults (no providers, 'real', cap 0): the tenant gets no answers until the operator sets them.
    await session.execute(
        text("INSERT INTO grounded.tenant_settings (tenant_id) VALUES (:tenant_id)"),
        {"tenant_id": tenant_id},
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        action="tenant.create",
        target_type="tenant",
        target_id=tenant_id,
        detail={},
    )
    return tenant_id


async def add_domain(session: AsyncSession, *, tenant_slug: str, domain: str) -> None:
    """Allow employees to be created at this domain for this tenant."""
    tenant_id = await _tenant_id_for_slug(session, normalize_slug(tenant_slug))
    clean_domain = normalize_domain(domain)
    domain_id = uuid.uuid4()
    try:
        await session.execute(
            text(
                "INSERT INTO grounded.tenant_email_domains (id, tenant_id, domain) "
                "VALUES (:id, :tenant_id, :domain)"
            ),
            {"id": domain_id, "tenant_id": tenant_id, "domain": clean_domain},
        )
    except IntegrityError as exc:
        if _is_unique_violation(exc):
            raise AdminError(f"'{clean_domain}' is already allowed for this tenant.") from exc
        raise
    await _audit(
        session,
        tenant_id=tenant_id,
        action="tenant_domain.add",
        target_type="tenant_email_domain",
        target_id=domain_id,
        detail={"domain": clean_domain},
    )


async def remove_domain(session: AsyncSession, *, tenant_slug: str, domain: str) -> None:
    """
    Remove a domain from the allowlist.

    Checks the result (ADR 0006 carry-over, PROGRESS Session 3). A DELETE that matches no row
    returns no error, only zero rows. RETURNING shows whether a row went, so "nothing deleted"
    is a refusal here, never a silent success.
    """
    tenant_id = await _tenant_id_for_slug(session, normalize_slug(tenant_slug))
    clean_domain = normalize_domain(domain)
    result = await session.execute(
        text(
            "DELETE FROM grounded.tenant_email_domains "
            "WHERE tenant_id = :tenant_id AND domain = :domain RETURNING id"
        ),
        {"tenant_id": tenant_id, "domain": clean_domain},
    )
    deleted = result.first()
    if deleted is None:
        raise AdminError(f"'{clean_domain}' is not on this tenant's allowlist. Nothing changed.")
    await _audit(
        session,
        tenant_id=tenant_id,
        action="tenant_domain.remove",
        target_type="tenant_email_domain",
        target_id=deleted.id,
        detail={"domain": clean_domain},
    )


async def list_domains(session: AsyncSession, *, tenant_slug: str) -> list[str]:
    """Return the allowlisted domains for a tenant, sorted."""
    tenant_id = await _tenant_id_for_slug(session, normalize_slug(tenant_slug))
    result = await session.execute(
        text(
            "SELECT domain FROM grounded.tenant_email_domains "
            "WHERE tenant_id = :tenant_id ORDER BY domain"
        ),
        {"tenant_id": tenant_id},
    )
    return [row.domain for row in result]


async def _require_allowed_domain(
    session: AsyncSession, *, tenant_id: uuid.UUID, email: str
) -> None:
    """Refuse an email whose domain is not on this tenant's allowlist."""
    domain = email.split("@", 1)[1]
    result = await session.execute(
        text(
            "SELECT 1 FROM grounded.tenant_email_domains "
            "WHERE tenant_id = :tenant_id AND domain = :domain"
        ),
        {"tenant_id": tenant_id, "domain": domain},
    )
    if result.first() is None:
        raise AdminError(
            f"'{domain}' is not on this tenant's allowlist. Run add-domain first. Nothing changed."
        )


async def check_admin_target(session: AsyncSession, *, tenant_slug: str, email: str) -> str:
    """
    Check the tenant and the email domain before the CLI asks for a password. Writes nothing.

    The create step checks the same rules again inside its own transaction, so a change between
    the two calls is still refused.
    """
    tenant_id = await _tenant_id_for_slug(session, normalize_slug(tenant_slug))
    clean_email = normalize_email(email)
    await _require_allowed_domain(session, tenant_id=tenant_id, email=clean_email)
    return clean_email


async def _insert_membership(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> None:
    await session.execute(
        text(
            "INSERT INTO grounded.tenant_memberships (id, tenant_id, user_id, role) "
            "VALUES (:id, :tenant_id, :user_id, :role)"
        ),
        {"id": uuid.uuid4(), "tenant_id": tenant_id, "user_id": user_id, "role": role},
    )


async def create_tenant_admin(
    session: AsyncSession,
    *,
    tenant_slug: str,
    email: str,
    display_name: str,
    password: str,
) -> uuid.UUID:
    """
    Create the first tenant_admin for a tenant: user row, membership, and audit row.

    The user and the membership are written in the caller's transaction. If the membership
    insert fails, the user insert rolls back too, so no membership-less user is left behind
    (ADR 0004). The email domain must already be on the tenant's allowlist.
    """
    tenant_id = await _tenant_id_for_slug(session, normalize_slug(tenant_slug))
    clean_email = normalize_email(email)
    clean_name = normalize_display_name(display_name)
    try:
        check_password_policy(password)
    except PasswordPolicyError as exc:
        raise AdminError(str(exc)) from exc
    await _require_allowed_domain(session, tenant_id=tenant_id, email=clean_email)

    user_id = uuid.uuid4()
    try:
        await session.execute(
            text(
                "INSERT INTO grounded.users (id, email, display_name, password_hash) "
                "VALUES (:id, :email, :display_name, :password_hash)"
            ),
            {
                "id": user_id,
                "email": clean_email,
                "display_name": clean_name,
                "password_hash": hash_password(password),
            },
        )
    except IntegrityError as exc:
        if _is_unique_violation(exc):
            raise AdminError("An account with this email already exists. Nothing changed.") from exc
        raise
    await _insert_membership(session, tenant_id=tenant_id, user_id=user_id, role="tenant_admin")
    await _audit(
        session,
        tenant_id=tenant_id,
        action="user.create",
        target_type="user",
        target_id=user_id,
        detail={"role": "tenant_admin"},
    )
    return user_id


async def record_account_unlock(
    session: AsyncSession, *, tenant_slug: str, keys_removed: int
) -> None:
    """
    Audit an operator unlock (ADR 0014). The detail holds the count of keys removed, never the email.
    The Valkey keys are removed before this row is written. If this write fails, the CLI says so.
    """
    tenant_id = await _tenant_id_for_slug(session, normalize_slug(tenant_slug))
    await _audit(
        session,
        tenant_id=tenant_id,
        action="account.unlock",
        target_type="account",
        target_id=None,
        detail={"keys_removed": keys_removed},
    )


MICRO_PER_USD = 1_000_000
MAX_DAILY_CAP_MICRO_USD = 1_000_000_000  # $1,000: the same bound as the table's CHECK.


@dataclass(frozen=True)
class TenantSettingsView:
    answer_providers: tuple[str, ...]
    data_class: str
    daily_cost_cap_micro_usd: int


def usd_to_micro(raw: str) -> int:
    """Dollars as typed (for example 0.05) to whole millionths of a dollar. Decimal, never float."""
    try:
        amount = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise AdminError("The daily cap must be a number of US dollars, for example 0.05.") from exc
    if not amount.is_finite():
        raise AdminError("The daily cap must be a number of US dollars, for example 0.05.")
    micro = amount * MICRO_PER_USD
    if micro != micro.to_integral_value():
        raise AdminError("The daily cap can have at most 6 decimal places.")
    if not 0 <= micro <= MAX_DAILY_CAP_MICRO_USD:
        raise AdminError("The daily cap must be between 0 and 1000 US dollars.")
    return int(micro)


def micro_to_usd(micro: int) -> str:
    return f"{Decimal(micro) / MICRO_PER_USD:.6f}"


async def _read_settings(session: AsyncSession, tenant_id: uuid.UUID) -> TenantSettingsView | None:
    result = await session.execute(
        text(
            "SELECT answer_providers, data_class, daily_cost_cap_micro_usd "
            "FROM grounded.tenant_settings WHERE tenant_id = :tenant_id"
        ),
        {"tenant_id": tenant_id},
    )
    row = result.first()
    if row is None:
        return None
    return TenantSettingsView(
        tuple(row.answer_providers), row.data_class, int(row.daily_cost_cap_micro_usd)
    )


async def show_tenant_settings(session: AsyncSession, *, tenant_slug: str) -> TenantSettingsView:
    """The tenant's answer settings. A tenant with no row is shown with the fail-closed defaults."""
    tenant_id = await _tenant_id_for_slug(session, normalize_slug(tenant_slug))
    return await _read_settings(session, tenant_id) or TenantSettingsView((), "real", 0)


async def update_tenant_settings(
    session: AsyncSession,
    *,
    tenant_slug: str,
    providers: Sequence[str] | None,
    data_class: str | None,
    daily_cap_micro_usd: int | None,
) -> dict[str, dict[str, Any]]:
    """
    Change a tenant's answer settings, check the result against the provider policy, and audit what changed.

    None means "leave as it is". The policy (answering/policy.py) is checked on the settings as they will be, not
    only on the new values: moving a tenant to 'real' while Groq is still listed is refused. A change that alters
    nothing writes no audit row. The audit detail holds the old and new values of the fields that changed:
    provider names, the data class and the cap. It holds no secret and no text.
    Returns the changes (empty when nothing changed).
    """
    tenant_id = await _tenant_id_for_slug(session, normalize_slug(tenant_slug))
    old = await _read_settings(session, tenant_id) or TenantSettingsView((), "real", 0)
    try:
        new_data_class = check_data_class(data_class) if data_class is not None else old.data_class
        new_providers = check_providers(
            providers if providers is not None else old.answer_providers, new_data_class
        )
    except ProviderPolicyError as exc:
        raise AdminError(f"{exc} Nothing changed.") from exc
    new_cap = old.daily_cost_cap_micro_usd if daily_cap_micro_usd is None else daily_cap_micro_usd
    if not 0 <= new_cap <= MAX_DAILY_CAP_MICRO_USD:
        raise AdminError("The daily cap must be between 0 and 1000 US dollars. Nothing changed.")
    new = TenantSettingsView(new_providers, new_data_class, new_cap)

    changes: dict[str, dict[str, Any]] = {}
    if new.answer_providers != old.answer_providers:
        changes["answer_providers"] = {
            "old": list(old.answer_providers),
            "new": list(new.answer_providers),
        }
    if new.data_class != old.data_class:
        changes["data_class"] = {"old": old.data_class, "new": new.data_class}
    if new.daily_cost_cap_micro_usd != old.daily_cost_cap_micro_usd:
        changes["daily_cost_cap_micro_usd"] = {
            "old": old.daily_cost_cap_micro_usd,
            "new": new.daily_cost_cap_micro_usd,
        }
    if not changes:
        return changes
    await session.execute(
        text(
            "INSERT INTO grounded.tenant_settings "
            "(tenant_id, answer_providers, data_class, daily_cost_cap_micro_usd) "
            "VALUES (:tenant_id, :providers, :data_class, :cap) "
            "ON CONFLICT (tenant_id) DO UPDATE SET answer_providers = EXCLUDED.answer_providers, "
            "data_class = EXCLUDED.data_class, "
            "daily_cost_cap_micro_usd = EXCLUDED.daily_cost_cap_micro_usd"
        ),
        {
            "tenant_id": tenant_id,
            "providers": list(new.answer_providers),
            "data_class": new.data_class,
            "cap": new.daily_cost_cap_micro_usd,
        },
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        action="tenant_settings.update",
        target_type="tenant",
        target_id=tenant_id,
        detail={"changes": changes},
    )
    return changes

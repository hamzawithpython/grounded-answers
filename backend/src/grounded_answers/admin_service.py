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

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
    target_id: uuid.UUID,
    detail: dict[str, str | int],
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

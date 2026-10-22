"""
Login and per-request membership checks (ADR 0004, Login flow and Per-request checks).

Login, in order:
1. auth_login_lookup(email) on an anonymous transaction (no tenant context). It is the only
   pre-tenant read, and it is a SECURITY DEFINER function (ADR 0004).
2. Argon2 verify on EVERY path. An unknown email, or a user without a usable hash, is verified
   against the dummy hash created at startup. The verify runs in a worker thread, so it never
   blocks the event loop.
3. Checks after the verify: user active, membership present and active, tenant active. All of
   them lead to the same LoginFailed, which the route turns into one generic 401.
4. Success: inside a tenant transaction (begin_tenant_transaction), write the refresh token
   (hashed) and an audit row. Failure for a known user: an audit row only. Failure for an
   unknown email: a log line with the HMAC'd client IP and no email address.

Nothing in this module logs or returns a password, a token, or an email address.
"""

import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from grounded_answers.auth_keys import KeyRing
from grounded_answers.auth_tokens import issue_access_token
from grounded_answers.models import AuditLog, RefreshToken
from grounded_answers.passwords import verify_password
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction

logger = logging.getLogger("grounded_answers.auth")

REFRESH_TOKEN_LIFETIME = timedelta(days=14)
# The single message every login failure returns (byte-identical body, same status).
LOGIN_FAILED_MESSAGE = "Invalid email or password"

LOOKUP_SQL = text(
    "SELECT user_id, password_hash, user_active, tenant_id, tenant_status, "
    "membership_role, membership_active FROM grounded.auth_login_lookup(:email)"
)


class LoginFailed(Exception):
    """Every reason to refuse a login. The route returns the same response for all of them."""


class MembershipRejected(Exception):
    """The request's token names a user, tenant or membership that is not usable."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class Principal:
    """The verified caller, with the role read from the database (never from the token)."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str


@dataclass(frozen=True)
class LoginResult:
    access_token: str
    refresh_token: str
    principal: Principal


@dataclass(frozen=True)
class LookupRow:
    user_id: uuid.UUID
    password_hash: str | None
    user_active: bool
    tenant_id: uuid.UUID | None
    tenant_status: str | None
    membership_role: str | None
    membership_active: bool | None


def hmac_client_ip(key: bytes, client_ip: str) -> bytes:
    """Keyed hash of the IP (ADR 0004). A plain hash of IPv4 could be reversed by brute force."""
    return hmac.new(key, client_ip.encode("ascii", "replace"), hashlib.sha256).digest()


def _hash_refresh_token(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("ascii")).digest()


async def _lookup(
    session_factory: async_sessionmaker[AsyncSession], email: str
) -> LookupRow | None:
    async with session_factory() as session, session.begin():
        row = (await session.execute(LOOKUP_SQL, {"email": email})).first()
    if row is None:
        return None
    return LookupRow(
        user_id=row.user_id,
        password_hash=row.password_hash,
        user_active=row.user_active,
        tenant_id=row.tenant_id,
        tenant_status=row.tenant_status,
        membership_role=row.membership_role,
        membership_active=row.membership_active,
    )


async def add_audit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    ip_hmac: bytes,
    detail: dict[str, Any] | None,
) -> None:
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_user_id=actor_id,
            action=action,
            target_type="user",
            target_id=actor_id,
            ip_hmac=ip_hmac,
            detail=detail,
        )
    )


def _log_failure(reason: str, ip_hmac: bytes) -> None:
    """Application log for failures with no tenant (ADR 0004). Event, reason, keyed IP only."""
    logger.info("login.failed reason=%s ip_hmac=%s", reason, ip_hmac.hex()[:16])


async def login(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    ring: KeyRing,
    dummy_hash: str,
    hmac_key: bytes,
    email: str,
    password: str,
    client_ip: str,
) -> LoginResult:
    """Return tokens for valid credentials. Raise LoginFailed for every other case."""
    ip_hmac = hmac_client_ip(hmac_key, client_ip)
    clean_email = email.strip().lower()
    row = await _lookup(session_factory, clean_email)

    stored_hash = row.password_hash if row is not None and row.password_hash else dummy_hash
    password_ok = await run_in_threadpool(verify_password, stored_hash, password)

    if row is None:
        _log_failure("unknown_email", ip_hmac)
        raise LoginFailed
    if not row.password_hash:
        _log_failure("no_password_set", ip_hmac)
        raise LoginFailed

    reason: str | None = None
    if not password_ok:
        reason = "password"
    elif not row.user_active:
        reason = "user_inactive"
    elif row.tenant_id is None or row.membership_role is None:
        reason = "no_membership"
    elif not row.membership_active:
        reason = "membership_inactive"
    elif row.tenant_status != "active":
        reason = "tenant_suspended"

    if reason is not None:
        if row.tenant_id is not None:
            context = TenantContext(tenant_id=row.tenant_id, user_id=row.user_id)
            async with begin_tenant_transaction(session_factory, context) as session:
                await add_audit(
                    session,
                    tenant_id=row.tenant_id,
                    actor_id=row.user_id,
                    action="login.failure",
                    ip_hmac=ip_hmac,
                    detail={"reason": reason},
                )
        _log_failure(reason, ip_hmac)
        raise LoginFailed

    tenant_id = row.tenant_id
    role = row.membership_role
    if tenant_id is None or role is None:
        # Unreachable: the reason checks above already refused a missing membership.
        raise LoginFailed
    raw_refresh = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    context = TenantContext(tenant_id=tenant_id, user_id=row.user_id)
    async with begin_tenant_transaction(session_factory, context) as session:
        session.add(
            RefreshToken(
                tenant_id=tenant_id,
                user_id=row.user_id,
                family_id=uuid.uuid4(),
                token_hash=_hash_refresh_token(raw_refresh),
                issued_at=now,
                expires_at=now + REFRESH_TOKEN_LIFETIME,
            )
        )
        await add_audit(
            session,
            tenant_id=tenant_id,
            actor_id=row.user_id,
            action="login.success",
            ip_hmac=ip_hmac,
            detail=None,
        )

    principal = Principal(user_id=row.user_id, tenant_id=tenant_id, role=role)
    access = issue_access_token(
        ring=ring,
        user_id=row.user_id,
        tenant_id=tenant_id,
        role_hint=role,
        now=now,
    )
    return LoginResult(access_token=access, refresh_token=raw_refresh, principal=principal)


async def load_principal(session: AsyncSession, context: TenantContext) -> Principal:
    """
    Re-check the caller inside the tenant transaction (ADR 0004, per-request checks).

    RLS limits the rows to the session's tenant, so a forged tenant id finds no membership.
    The role comes from the row, never from the token.
    """
    result = await session.execute(
        text(
            "SELECT m.role, m.active AS membership_active, t.status, u.active AS user_active "
            "FROM grounded.tenant_memberships AS m "
            "JOIN grounded.tenants AS t ON t.id = m.tenant_id "
            "JOIN grounded.users AS u ON u.id = m.user_id "
            "WHERE m.user_id = :user_id AND m.tenant_id = :tenant_id"
        ),
        {"user_id": context.user_id, "tenant_id": context.tenant_id},
    )
    row = result.first()
    if row is None:
        raise MembershipRejected(401, "Not authenticated")
    if not row.user_active or not row.membership_active:
        raise MembershipRejected(401, "Not authenticated")
    if row.status != "active":
        raise MembershipRejected(403, "Tenant access is suspended")
    return Principal(user_id=context.user_id, tenant_id=context.tenant_id, role=row.role)

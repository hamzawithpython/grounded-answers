"""
Refresh, rotation, reuse detection, and logout (ADR 0004, ADR 0013).

Refresh, in order:
1. Hash the cookie value, then auth_refresh_lookup (anonymous transaction; no lock yet).
2. Open the tenant transaction (begin_tenant_transaction) and lock the token row with
   SELECT ... FOR UPDATE. A second request with the same token waits here. When it gets the lock,
   the first request has committed, so it sees the row as used.
3. Re-check membership, user and tenant status inside the transaction (D4A: a suspended tenant or
   an inactive membership revokes the family).
4. Decide on the locked row, and do all writes inside the transaction:
   - Expired or revoked: refuse (401). Nothing changes.
   - Used less than 10 seconds ago (grace window): 409 refresh_in_progress. Nothing changes.
   - Used more than 10 seconds ago: reuse. Revoke the family, audit it, refuse (401).
   - Unused: insert the new token in the same family, then mark the old one used with
     replaced_by pointing at the new one. This UPDATE must affect exactly one row (D2A).
5. The transaction commits, and only then does the route send the response. A refusal that
   revoked something has already committed.

The family has an absolute cap (D3B): a rotated token never expires later than 14 days after the
family's first token, so a family cannot be kept alive indefinitely.

Logout and logout-all are idempotent. Writes that may legitimately affect zero rows (revoking a
family that is already revoked) do not use the exact-count check.
"""

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from grounded_answers.auth_keys import KeyRing
from grounded_answers.auth_service import (
    REFRESH_TOKEN_LIFETIME,
    MembershipRejected,
    Principal,
    add_audit,
    hmac_client_ip,
    load_principal,
)
from grounded_answers.auth_tokens import issue_access_token
from grounded_answers.models import RefreshToken
from grounded_answers.rowcount import affected_rows, expect_rowcount
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction

logger = logging.getLogger("grounded_answers.auth")

REFRESH_GRACE = timedelta(seconds=10)

LOOKUP_SQL = text(
    "SELECT token_id, user_id, tenant_id, family_id FROM grounded.auth_refresh_lookup(:token_hash)"
)
LOCK_SQL = text(
    "SELECT id, user_id, family_id, used_at, revoked_at, expires_at "
    "FROM grounded.refresh_tokens WHERE id = :id FOR UPDATE"
)
FAMILY_ORIGIN_SQL = text(
    "SELECT min(issued_at) FROM grounded.refresh_tokens WHERE family_id = :family_id"
)
REVOKE_FAMILY_SQL = text(
    "UPDATE grounded.refresh_tokens SET revoked_at = :now "
    "WHERE family_id = :family_id AND revoked_at IS NULL"
)
MARK_USED_SQL = text(
    "UPDATE grounded.refresh_tokens SET used_at = :now, replaced_by = :new_id "
    "WHERE id = :id AND used_at IS NULL"
)
REVOKE_USER_SQL = text(
    "UPDATE grounded.refresh_tokens SET revoked_at = :now "
    "WHERE user_id = :user_id AND revoked_at IS NULL"
)


class RefreshFailed(Exception):
    """Refused. The route answers with this status and clears the cookie."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class RefreshInProgress(Exception):
    """Presented again within the grace window. The route answers 409 and keeps the cookie."""


@dataclass(frozen=True)
class RefreshResult:
    access_token: str
    refresh_token: str
    expires_at: datetime


@dataclass(frozen=True)
class _Decision:
    kind: str  # "ok", "in_progress", or "refused"
    result: RefreshResult | None = None
    status_code: int = 401
    detail: str = "Not authenticated"


def next_expiry(now: datetime, family_origin: datetime) -> datetime:
    """The expiry of a rotated token: 14 days from now, but never past the family's cap (D3B)."""
    cap = family_origin + REFRESH_TOKEN_LIFETIME
    return min(now + REFRESH_TOKEN_LIFETIME, cap)


def hash_refresh_token(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("ascii")).digest()


async def _lock_token(session: AsyncSession, token_id: uuid.UUID) -> Any:
    """Lock the token row until the transaction ends. Tests replace this to force a race."""
    result = await session.execute(LOCK_SQL, {"id": token_id})
    row = result.first()
    if row is None:
        raise RefreshFailed(401, "Not authenticated")
    return row


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID, now: datetime) -> int:
    result = await session.execute(REVOKE_FAMILY_SQL, {"now": now, "family_id": family_id})
    return affected_rows(result)


async def _decide(
    session: AsyncSession,
    *,
    token_id: uuid.UUID,
    ctx: TenantContext,
    ring: KeyRing,
    ip_hmac: bytes,
    now: datetime,
) -> _Decision:
    """Everything that happens after the lock. Runs inside the tenant transaction."""
    token = await _lock_token(session, token_id)
    family_id: uuid.UUID = token.family_id

    try:
        principal: Principal = await load_principal(session, ctx)
    except MembershipRejected as exc:
        revoked = await _revoke_family(session, family_id, now)
        reason = "tenant_suspended" if exc.status_code == 403 else "account_inactive"
        await add_audit(
            session,
            tenant_id=ctx.tenant_id,
            actor_id=ctx.user_id,
            action="refresh.refused",
            ip_hmac=ip_hmac,
            detail={"reason": reason, "revoked_tokens": revoked},
        )
        logger.info("refresh.refused reason=%s", reason)
        return _Decision(kind="refused", status_code=exc.status_code, detail=exc.detail)

    if token.revoked_at is not None or token.expires_at <= now:
        return _Decision(kind="refused")

    if token.used_at is not None:
        if now - token.used_at < REFRESH_GRACE:
            return _Decision(kind="in_progress")
        revoked = await _revoke_family(session, family_id, now)
        await add_audit(
            session,
            tenant_id=ctx.tenant_id,
            actor_id=ctx.user_id,
            action="refresh.reuse_detected",
            ip_hmac=ip_hmac,
            detail={"revoked_tokens": revoked},
        )
        logger.info("refresh.reuse_detected family=%s revoked=%d", str(family_id)[:8], revoked)
        return _Decision(kind="refused")

    origin_row = (await session.execute(FAMILY_ORIGIN_SQL, {"family_id": family_id})).scalar_one()
    expires_at = next_expiry(now, origin_row)
    if expires_at <= now:
        return _Decision(kind="refused")

    new_raw = secrets.token_urlsafe(32)
    new_id = uuid.uuid4()
    session.add(
        RefreshToken(
            id=new_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            family_id=family_id,
            token_hash=hash_refresh_token(new_raw),
            issued_at=now,
            expires_at=expires_at,
        )
    )
    await session.flush()
    mark = await session.execute(MARK_USED_SQL, {"now": now, "new_id": new_id, "id": token.id})
    expect_rowcount(mark, 1, "refresh.mark_used")

    access = issue_access_token(
        ring=ring,
        user_id=ctx.user_id,
        tenant_id=ctx.tenant_id,
        role_hint=principal.role,
        now=now,
    )
    return _Decision(
        kind="ok",
        result=RefreshResult(access_token=access, refresh_token=new_raw, expires_at=expires_at),
    )


async def refresh(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    ring: KeyRing,
    raw_refresh: str | None,
    hmac_key: bytes,
    client_ip: str,
) -> RefreshResult:
    """Rotate the refresh token. Raise RefreshFailed or RefreshInProgress; never return partial data."""
    ip_hmac = hmac_client_ip(hmac_key, client_ip)
    if not raw_refresh:
        raise RefreshFailed(401, "Not authenticated")
    async with session_factory() as session, session.begin():
        found = (
            await session.execute(LOOKUP_SQL, {"token_hash": hash_refresh_token(raw_refresh)})
        ).first()
    if found is None:
        logger.info("refresh.unknown_token")
        raise RefreshFailed(401, "Not authenticated")

    ctx = TenantContext(tenant_id=found.tenant_id, user_id=found.user_id)
    now = datetime.now(UTC)
    async with begin_tenant_transaction(session_factory, ctx) as session:
        decision = await _decide(
            session, token_id=found.token_id, ctx=ctx, ring=ring, ip_hmac=ip_hmac, now=now
        )

    if decision.kind == "in_progress":
        raise RefreshInProgress
    if decision.kind == "refused" or decision.result is None:
        raise RefreshFailed(decision.status_code, decision.detail)
    return decision.result


async def logout(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    raw_refresh: str | None,
    hmac_key: bytes,
    client_ip: str,
) -> None:
    """Revoke the cookie's family. Idempotent: an unknown or missing cookie is not an error."""
    if not raw_refresh:
        return
    async with session_factory() as session, session.begin():
        found = (
            await session.execute(LOOKUP_SQL, {"token_hash": hash_refresh_token(raw_refresh)})
        ).first()
    if found is None:
        return
    ctx = TenantContext(tenant_id=found.tenant_id, user_id=found.user_id)
    now = datetime.now(UTC)
    ip_hmac = hmac_client_ip(hmac_key, client_ip)
    async with begin_tenant_transaction(session_factory, ctx) as session:
        revoked = await _revoke_family(session, found.family_id, now)
        await add_audit(
            session,
            tenant_id=ctx.tenant_id,
            actor_id=ctx.user_id,
            action="logout",
            ip_hmac=ip_hmac,
            detail={"revoked_tokens": revoked},
        )


async def logout_all(
    session: AsyncSession,
    *,
    principal: Principal,
    ip_hmac: bytes,
) -> int:
    """Revoke every family of this user in this tenant. Runs in the caller's tenant transaction."""
    now = datetime.now(UTC)
    result = await session.execute(REVOKE_USER_SQL, {"now": now, "user_id": principal.user_id})
    revoked = affected_rows(result)
    await add_audit(
        session,
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        action="logout_all",
        ip_hmac=ip_hmac,
        detail={"revoked_tokens": revoked},
    )
    return revoked

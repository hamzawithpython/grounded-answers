"""
Access tokens: EdDSA JWTs, 15 minutes (ADR 0004, Tokens).

Verification rules, in order:
1. The algorithm is pinned to EdDSA. PyJWT refuses any other header value, including "none",
   and an HS256 token signed with the public key bytes (algorithm confusion).
2. The kid header picks a key from the ring. An unknown kid is refused before any signature check.
3. The signature, exp, iat, iss and aud are checked. 5 seconds of leeway covers clock skew between
   API instances (decision 3). The required claims must all be present.
4. The claims are hints. The auth dependency re-reads membership and tenant status from the
   database for every request (ADR 0004, per-request checks).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from grounded_answers.auth_keys import KeyRing

ISSUER = "grounded-answers-api"
AUDIENCE = "grounded-answers-web"
ALGORITHM = "EdDSA"
ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
LEEWAY = timedelta(seconds=5)
REQUIRED_CLAIMS = ["sub", "tid", "iat", "exp", "iss", "aud", "jti"]


class InvalidAccessToken(Exception):
    """Any reason to refuse an access token. The message is for logs only, never for clients."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    token_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime
    # A hint for the UI. Authorization uses the role from the database, never this value.
    role_hint: str | None


def issue_access_token(
    *,
    ring: KeyRing,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role_hint: str,
    now: datetime | None = None,
) -> str:
    issued = now or datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role_hint,
        "iat": issued,
        "exp": issued + ACCESS_TOKEN_LIFETIME,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "jti": str(uuid.uuid4()),
    }
    token: str = jwt.encode(
        payload,
        ring.signing_key,
        algorithm=ALGORITHM,
        headers={"kid": ring.active_kid},
    )
    return token


def verify_access_token(token: str, ring: KeyRing) -> AccessClaims:
    """Return the claims, or raise InvalidAccessToken. Never returns partial data."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise InvalidAccessToken("malformed header") from exc
    kid = header.get("kid")
    key = ring.verification_keys.get(kid) if isinstance(kid, str) else None
    if key is None:
        raise InvalidAccessToken("unknown kid")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            leeway=LEEWAY,
            options={"require": REQUIRED_CLAIMS},
        )
    except jwt.InvalidTokenError as exc:
        raise InvalidAccessToken(type(exc).__name__) from exc
    try:
        return AccessClaims(
            user_id=uuid.UUID(str(claims["sub"])),
            tenant_id=uuid.UUID(str(claims["tid"])),
            token_id=uuid.UUID(str(claims["jti"])),
            issued_at=datetime.fromtimestamp(float(claims["iat"]), UTC),
            expires_at=datetime.fromtimestamp(float(claims["exp"]), UTC),
            role_hint=claims["role"] if isinstance(claims.get("role"), str) else None,
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise InvalidAccessToken("malformed claims") from exc

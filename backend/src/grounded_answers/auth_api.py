"""
HTTP routes for authentication (ADR 0004): POST /auth/login and GET /auth/me.

Responses:
- Every login failure is the same 401 with the same JSON body and no extra headers.
- Every authentication failure on a protected route is a 401 "Not authenticated" with a
  WWW-Authenticate header, or a 403 for a suspended tenant (ADR 0004: suspension is shown only
  to already-authenticated users).
- The refresh cookie is `__Secure-refresh`: HttpOnly, Secure, SameSite=Lax, Path=/auth, no
  Domain. The __Secure- prefix makes browsers refuse the cookie unless Secure is set.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from grounded_answers.auth_service import (
    LOGIN_FAILED_MESSAGE,
    REFRESH_TOKEN_LIFETIME,
    LoginFailed,
    MembershipRejected,
    Principal,
    load_principal,
    login,
)
from grounded_answers.auth_tokens import (
    ACCESS_TOKEN_LIFETIME,
    InvalidAccessToken,
    verify_access_token,
)
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction

logger = logging.getLogger("grounded_answers.auth")

REFRESH_COOKIE = "__Secure-refresh"
REFRESH_COOKIE_PATH = "/auth"

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    # Lengths only. An invalid email is not a 422: it fails the lookup and gets the generic 401.
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class PrincipalResponse(BaseModel):
    user_id: str
    tenant_id: str
    role: str


@dataclass(frozen=True)
class RequestContext:
    """What a protected route receives: a tenant transaction and the verified caller."""

    session: AsyncSession
    principal: Principal


def _login_failed() -> JSONResponse:
    # One body, one status, no extra headers. Every LoginFailed reason produces exactly this.
    return JSONResponse(status_code=401, content={"detail": LOGIN_FAILED_MESSAGE})


def _not_authenticated() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise _not_authenticated()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or " " in token:
        raise _not_authenticated()
    return token


async def authenticated_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AsyncIterator[RequestContext]:
    """
    Verify the bearer token, open the tenant transaction, and re-check the caller in the database.

    The token's tid and sub are only used to open the transaction. The membership and tenant
    checks run inside it, so RLS applies to them, and the role is read from the row.
    """
    token = _bearer_token(authorization)
    try:
        claims = verify_access_token(token, request.app.state.key_ring)
    except InvalidAccessToken as exc:
        # The reason stays server-side. Clients get one message.
        logger.info("access.rejected reason=%s", exc)
        raise _not_authenticated() from exc

    context = TenantContext(tenant_id=claims.tenant_id, user_id=claims.user_id)
    async with begin_tenant_transaction(request.app.state.session_factory, context) as session:
        try:
            principal = await load_principal(session, context)
        except MembershipRejected as exc:
            if exc.status_code == 401:
                raise _not_authenticated() from exc
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        yield RequestContext(session=session, principal=principal)


AuthenticatedRequest = Annotated[
    RequestContext,
    Depends(authenticated_context, scope="function"),
]


@router.post("/login", response_model=None)
async def login_route(body: LoginRequest, request: Request) -> Response:
    state = request.app.state
    client_ip = request.client.host if request.client is not None else "unknown"
    try:
        result = await login(
            session_factory=state.session_factory,
            ring=state.key_ring,
            dummy_hash=state.dummy_hash,
            hmac_key=state.auth_hmac_key,
            email=body.email,
            password=body.password,
            client_ip=client_ip,
        )
    except LoginFailed:
        return _login_failed()

    response = JSONResponse(
        content={
            "access_token": result.access_token,
            "token_type": "bearer",
            "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
        },
    )
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=result.refresh_token,
        max_age=int(REFRESH_TOKEN_LIFETIME.total_seconds()),
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/me", response_model=PrincipalResponse)
async def me(ctx: AuthenticatedRequest) -> PrincipalResponse:
    return PrincipalResponse(
        user_id=str(ctx.principal.user_id),
        tenant_id=str(ctx.principal.tenant_id),
        role=ctx.principal.role,
    )

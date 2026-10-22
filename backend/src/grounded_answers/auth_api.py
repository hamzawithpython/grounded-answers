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
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from grounded_answers.auth_refresh import (
    RefreshFailed,
    RefreshInProgress,
    logout,
    logout_all,
    refresh,
)
from grounded_answers.auth_service import (
    LOGIN_FAILED_MESSAGE,
    REFRESH_TOKEN_LIFETIME,
    LoginFailed,
    MembershipRejected,
    Principal,
    hmac_client_ip,
    load_principal,
    login,
)
from grounded_answers.auth_tokens import (
    ACCESS_TOKEN_LIFETIME,
    InvalidAccessToken,
    verify_access_token,
)
from grounded_answers.config import get_settings
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


def _require_allowed_origin(request: Request) -> None:
    """
    CSRF check for the cookie-bearing POSTs (ADR 0004, D6B). The browser must send an Origin that is
    on the CORS allowlist. A missing Origin is refused too: browsers send one on every cross-site POST.
    """
    origin = request.headers.get("origin")
    if origin is None or origin not in get_settings().get_cors_origins():
        raise HTTPException(status_code=403, detail="Origin not allowed")


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


@router.post("/refresh", response_model=None)
async def refresh_route(request: Request) -> Response:
    _require_allowed_origin(request)
    state = request.app.state
    try:
        result = await refresh(
            session_factory=state.session_factory,
            ring=state.key_ring,
            raw_refresh=request.cookies.get(REFRESH_COOKIE),
            hmac_key=state.auth_hmac_key,
            client_ip=_client_ip(request),
        )
    except RefreshInProgress:
        # Presented again inside the grace window. The cookie stays: the first request already rotated it.
        return JSONResponse(status_code=409, content={"detail": "refresh_in_progress"})
    except RefreshFailed as exc:
        response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        _clear_refresh_cookie(response)
        return response

    remaining = int((result.expires_at - datetime.now(UTC)).total_seconds())
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
        max_age=remaining,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout", status_code=204, response_model=None)
async def logout_route(request: Request) -> Response:
    _require_allowed_origin(request)
    state = request.app.state
    await logout(
        session_factory=state.session_factory,
        raw_refresh=request.cookies.get(REFRESH_COOKIE),
        hmac_key=state.auth_hmac_key,
        client_ip=_client_ip(request),
    )
    response = Response(status_code=204)
    _clear_refresh_cookie(response)
    return response


@router.post("/logout-all", status_code=204, response_model=None)
async def logout_all_route(request: Request, ctx: AuthenticatedRequest) -> Response:
    await logout_all(
        ctx.session,
        principal=ctx.principal,
        ip_hmac=hmac_client_ip(request.app.state.auth_hmac_key, _client_ip(request)),
    )
    response = Response(status_code=204)
    _clear_refresh_cookie(response)
    return response

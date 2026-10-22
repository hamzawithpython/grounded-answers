"""
The only way to start a tenant transaction (ADR 0005, ADR 0004 per-request checks).

begin_tenant_transaction opens a new session and transaction. Its first two statements set the
tenant and the acting user for that transaction only, with set_config(..., true). The values
end at COMMIT or ROLLBACK, so a pooled connection never carries one request's tenant into the
next. Nothing else in the application may set app.current_tenant or app.current_user. A test
scans src/ to enforce that.

Why set_config and not SET: SET is session-scoped and survives on the pooled connection; it
also cannot take bind parameters. set_config is a function, so the values are bound, not
interpolated.

Why a fresh session per transaction (ADR 0005, decision D1): a second transaction on the same
session could keep state from the first. A new session cannot.
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# One statement, two bound values. Both are transaction-local (the third argument is true).
SET_TENANT_CONTEXT_SQL = text(
    "SELECT set_config('app.current_tenant', :tenant_id, true), "
    "set_config('app.current_user', :user_id, true)"
)

# The worker has no user, only a tenant (ADR 0017). The user setting stays unset. The only code
# that reads app.current_user is create_tenant_user, and worker_user has no EXECUTE on it.
SET_WORKER_CONTEXT_SQL = text("SELECT set_config('app.current_tenant', :tenant_id, true)")


@dataclass(frozen=True)
class TenantContext:
    """Who is acting, and in which tenant. Filled in by authentication (Step 3)."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID


@asynccontextmanager
async def begin_tenant_transaction(
    session_factory: async_sessionmaker[AsyncSession], context: TenantContext
) -> AsyncIterator[AsyncSession]:
    """
    Yield a session inside one transaction that already carries the tenant context.

    Commits when the block finishes normally. Rolls back on any exception, including one raised
    by the database. The exception is re-raised, never swallowed.
    """
    async with session_factory() as session, session.begin():
        await session.execute(
            SET_TENANT_CONTEXT_SQL,
            {"tenant_id": str(context.tenant_id), "user_id": str(context.user_id)},
        )
        yield session


@dataclass(frozen=True)
class WorkerContext:
    """The tenant a background job works for. A job has no user (ADR 0017)."""

    tenant_id: uuid.UUID


@asynccontextmanager
async def begin_worker_transaction(
    session_factory: async_sessionmaker[AsyncSession], context: WorkerContext
) -> AsyncIterator[AsyncSession]:
    """
    Yield a session inside one transaction that carries only the job's tenant (ADR 0017).

    The same rules as begin_tenant_transaction: a new session, a bound transaction-local value
    as the first statement, commit on success, rollback and re-raise on any exception.
    """
    async with session_factory() as session, session.begin():
        await session.execute(SET_WORKER_CONTEXT_SQL, {"tenant_id": str(context.tenant_id)})
        yield session


def make_tenant_session(
    get_context: Callable[..., Awaitable[TenantContext]],
) -> Callable[..., AsyncIterator[AsyncSession]]:
    """
    Build the FastAPI dependency that gives a route one tenant transaction.

    get_context is supplied by authentication (Step 3). It returns the TenantContext for the
    request, from a verified token and a membership check.

    Routes must declare the dependency with scope="function":

        session: Annotated[AsyncSession, Depends(make_tenant_session(get_ctx), scope="function")]

    With the default scope, FastAPI runs the code after yield after the response is sent, so the
    commit would happen too late to change the response. With "function", the commit happens
    before the response. A failed commit then becomes a server error, not a silent success.
    The session factory is read from app.state.session_factory (set in the app lifespan).
    """

    async def _tenant_session(
        request: Request,
        context: Annotated[TenantContext, Depends(get_context)],
    ) -> AsyncIterator[AsyncSession]:
        session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
        async with begin_tenant_transaction(session_factory, context) as session:
            yield session

    return _tenant_session

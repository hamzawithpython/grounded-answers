"""
The database side of POST /ask (ADR 0033): the tenant's settings and today's spend, retrieval, and the metering row.

Each method opens its own short tenant transaction as app_user (RLS applies) and closes it before returning, so no
connection is held while the model is called. Each sets a transaction-local `statement_timeout`, so a slow query ends
inside its stage budget instead of running on in the background. Any database error becomes StoreUnavailable: the flow
needs no driver types, and the error text, which can name a host or a column, is never passed on.
"""

import logging
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from grounded_answers.ask.types import (
    Caller,
    StoreUnavailable,
    TenantAnswerSettings,
    TenantState,
    UsageRecord,
)
from grounded_answers.retrieval import RetrievedChunk, retrieve
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction

logger = logging.getLogger(__name__)

_SETTINGS: Final = text(
    "SELECT answer_providers, data_class, daily_cost_cap_micro_usd "
    "FROM grounded.tenant_settings WHERE tenant_id = :tenant_id"
)
# Today, in UTC: the same day boundary the cap message promises. The (tenant_id, created_at) index serves it.
_SPENT_TODAY: Final = text(
    "SELECT COALESCE(sum(cost_micro_usd), 0)::bigint FROM grounded.answer_usage "
    "WHERE tenant_id = :tenant_id "
    "AND created_at >= (date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc')"
)
_INSERT_USAGE: Final = text(
    "INSERT INTO grounded.answer_usage (id, tenant_id, provider, model, outcome, attempts, retrieved_count, "
    "input_tokens, output_tokens, cost_micro_usd, latency_ms, invalid_codes) "
    "VALUES (:id, :tenant_id, :provider, :model, :outcome, :attempts, :retrieved_count, "
    ":input_tokens, :output_tokens, :cost_micro_usd, :latency_ms, :invalid_codes)"
)


class DbStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        state_timeout_ms: int = 2000,
        retrieve_timeout_ms: int = 3000,
        record_timeout_ms: int = 2000,
    ) -> None:
        self._factory = session_factory
        self._state_ms = state_timeout_ms
        self._retrieve_ms = retrieve_timeout_ms
        self._record_ms = record_timeout_ms

    @staticmethod
    def _context(caller: Caller) -> TenantContext:
        return TenantContext(tenant_id=caller.tenant_id, user_id=caller.user_id)

    @staticmethod
    async def _limit(session: AsyncSession, milliseconds: int) -> None:
        # SET cannot take a bound parameter. The value is an integer from our own configuration, never from a request,
        # and int() makes that true here too. (set_config is reserved for the tenant helper: tests/test_tenant_context_source.py.)
        await session.execute(text(f"SET LOCAL statement_timeout = {int(milliseconds)}"))

    async def read_state(self, caller: Caller) -> TenantState:
        try:
            async with begin_tenant_transaction(self._factory, self._context(caller)) as session:
                await self._limit(session, self._state_ms)
                row = (await session.execute(_SETTINGS, {"tenant_id": caller.tenant_id})).first()
                spent = int(
                    (
                        await session.execute(_SPENT_TODAY, {"tenant_id": caller.tenant_id})
                    ).scalar_one()
                )
        except (SQLAlchemyError, OSError) as exc:
            raise self._unavailable("read_state", exc) from exc
        settings = (
            None
            if row is None
            else TenantAnswerSettings(
                tuple(row.answer_providers), row.data_class, int(row.daily_cost_cap_micro_usd)
            )
        )
        return TenantState(settings, spent)

    async def retrieve(
        self, caller: Caller, question: str, vector: list[float], top_k: int
    ) -> list[RetrievedChunk]:
        try:
            async with begin_tenant_transaction(self._factory, self._context(caller)) as session:
                await self._limit(session, self._retrieve_ms)
                return await retrieve(session, question, vector, top_k=top_k)
        except (SQLAlchemyError, OSError) as exc:
            raise self._unavailable("retrieve", exc) from exc

    async def record(self, caller: Caller, record: UsageRecord) -> None:
        try:
            async with begin_tenant_transaction(self._factory, self._context(caller)) as session:
                await self._limit(session, self._record_ms)
                await session.execute(
                    _INSERT_USAGE,
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": caller.tenant_id,
                        "provider": record.provider,
                        "model": record.model,
                        "outcome": record.outcome,
                        "attempts": record.attempts,
                        "retrieved_count": record.retrieved_count,
                        "input_tokens": record.input_tokens,
                        "output_tokens": record.output_tokens,
                        "cost_micro_usd": record.cost_micro_usd,
                        "latency_ms": record.latency_ms,
                        "invalid_codes": list(record.invalid_codes),
                    },
                )
        except (SQLAlchemyError, OSError) as exc:
            raise self._unavailable("record", exc) from exc

    @staticmethod
    def _unavailable(operation: str, exc: Exception) -> StoreUnavailable:
        # The class name only: a driver message can hold a host, a column or a value.
        logger.warning("ask.store_failed operation=%s reason=%s", operation, type(exc).__name__)
        return StoreUnavailable(operation)

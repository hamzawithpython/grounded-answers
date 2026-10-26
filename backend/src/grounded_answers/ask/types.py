"""
The pieces of POST /ask and what the flow returns (ADR 0033).

The flow (service.py) is written against the small interfaces below, so its order, its error mapping and its
metering are tested without a database or Valkey. The real implementations come in Step 5c-2: Valkey for the
limiter, the in-flight set and the metering flag (valkey.py), and short database transactions for the store.

The flow returns a value for every expected outcome (answered, refused, limited, unavailable); the HTTP layer maps
them to status codes. An unexpected exception is metered as a service error and then raised.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol

from grounded_answers.answering.respond import GroundedAnswer, Refusal
from grounded_answers.answering.types import AnswerModel
from grounded_answers.retrieval import RetrievedChunk


@dataclass(frozen=True)
class Caller:
    """The verified caller: the tenant and the user, from the token and the database, never from the body."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID


@dataclass(frozen=True)
class TenantAnswerSettings:
    """A row of tenant_settings (migration 0017)."""

    answer_providers: tuple[str, ...]
    data_class: str
    daily_cost_cap_micro_usd: int


@dataclass(frozen=True)
class TenantState:
    """What one short transaction reads: the settings (None when the tenant has no row) and today's spend."""

    settings: TenantAnswerSettings | None
    spent_micro_usd: int


@dataclass(frozen=True)
class UsageRecord:
    """One row of answer_usage (migration 0017). Codes and numbers only: there is no field for text."""

    provider: str | None
    model: str | None
    outcome: str
    attempts: int
    retrieved_count: int
    input_tokens: int
    output_tokens: int
    cost_micro_usd: int
    latency_ms: int
    invalid_codes: tuple[str, ...]


class StoreUnavailable(Exception):
    """The database failed or refused. The store raises this for any database error, so the flow needs no driver."""


class Limiter(Protocol):
    async def admit(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> int | None:
        """Count one attempt. None when allowed, else the seconds to wait. Raises LimiterUnavailable."""
        ...


class InFlight(Protocol):
    async def acquire(self, tenant_id: uuid.UUID, request_id: str) -> bool:
        """Take one of the tenant's in-flight slots. False when all are taken. Raises LimiterUnavailable."""
        ...

    async def release(self, tenant_id: uuid.UUID, request_id: str) -> None: ...


class MeteringFlag(Protocol):
    async def is_set(self, tenant_id: uuid.UUID) -> bool: ...

    async def set(self, tenant_id: uuid.UUID) -> None: ...


class Store(Protocol):
    """Each method runs in its own short transaction, so no connection is held while the model is called."""

    async def read_state(self, caller: Caller) -> TenantState: ...

    async def retrieve(
        self, caller: Caller, question: str, vector: list[float], top_k: int
    ) -> list[RetrievedChunk]: ...

    async def record(self, caller: Caller, record: UsageRecord) -> None: ...


class Embedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class Providers(Protocol):
    def get(self, name: str) -> AnswerModel | None:
        """The adapter for a provider, or None when no key is configured for it."""
        ...


@dataclass(frozen=True)
class Answered:
    request_id: str
    answer: GroundedAnswer


@dataclass(frozen=True)
class Refused:
    request_id: str
    refusal: Refusal


@dataclass(frozen=True)
class Limited:
    """A limit stopped the request (HTTP 429). code: rate_limited, busy or daily_limit."""

    request_id: str
    code: str
    retry_after_seconds: int


@dataclass(frozen=True)
class Unavailable:
    """The service could not answer (HTTP 503). code: not_configured, try_again or service_unavailable."""

    request_id: str
    code: str
    retry_after_seconds: int | None


AskResult = Answered | Refused | Limited | Unavailable

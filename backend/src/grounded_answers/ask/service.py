"""
The flow of POST /ask, from a verified caller and a question to an answer, a refusal or a refusal to serve (ADR 0033).

Order, cheapest first, so a request that will be refused costs the least:

    1. rate limit per user and per tenant (Valkey)           -> Limited rate_limited
    2. metering-failure flag for the tenant (Valkey)         -> Unavailable service_unavailable
    3. in-flight slot for the tenant (Valkey)                -> Limited busy
    4. tenant settings and today's spend (one short transaction)
         no settings or no providers                         -> Unavailable not_configured (metered)
         spend at or over the daily cap                      -> Limited daily_limit
    5. provider policy and key                               -> Unavailable service_unavailable (metered)
    6. embed the question (CPU)                              -> Unavailable try_again / service_unavailable (metered)
    7. retrieve the chunks (a short transaction)             -> Unavailable (metered)
    8. answer_question: model, validation, one corrective retry
    9. the metering row, then the result

Every failure of steps 1 to 4's infrastructure (Valkey or the database unavailable, a timeout) answers 503 and calls no
model: nothing here can fall through to "allow". The slot taken in step 3 is released in every case, including an
exception and a cancelled request. No database connection is held while the model is called: each store method runs in
its own transaction.

Metering (step 9) runs for every request that passed the cap check, including refusals and failures after the model was
called. It is shielded from cancellation and tried twice. If both fail, the answer is still returned, a flag is set for
the tenant for a few minutes, and the next requests get 503 until it expires: a persistent metering failure must not
leave the cap reading zero (decision D5).

Logged, for every request: a request id, a keyed hash of the tenant id, an outcome code, counts, token counts and
timings, and error class names. Never the question, a source, an answer, a quote, a title, or a raw tenant or user id.
"""

import asyncio
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from grounded_answers.answering import policy
from grounded_answers.answering.pricing import UnknownModelPrice, cost_micro_usd
from grounded_answers.answering.respond import (
    AnswerDeadlineExceeded,
    AnswerOutcome,
    AnswerServiceError,
    GroundedAnswer,
    answer_question,
)
from grounded_answers.answering.types import (
    AnswerModel,
    ProviderRateLimited,
    ProviderTimeout,
    Usage,
)
from grounded_answers.answering.usage import outcome_for_refusal
from grounded_answers.ask.types import (
    Answered,
    AskResult,
    Caller,
    Embedder,
    InFlight,
    Limited,
    Limiter,
    MeteringFlag,
    Providers,
    Refused,
    Store,
    StoreUnavailable,
    TenantState,
    Unavailable,
    UsageRecord,
)
from grounded_answers.embedding.query import (
    QueryEmbedderBusy,
    QueryEmbedderNotReady,
    QueryEmbedderTimeout,
)
from grounded_answers.log_ref import tenant_log_ref
from grounded_answers.rate_limit import LimiterUnavailable

logger = logging.getLogger(__name__)

MAX_LATENCY_MS: Final = 600_000  # the CHECK on answer_usage.latency_ms
RELEASE_TIMEOUT_SECONDS: Final = 0.5
MAX_RETRY_AFTER_SECONDS: Final = 30
EMBEDDER_LOADING_RETRY_SECONDS: Final = 10
BUSY_RETRY_SECONDS: Final = 1


@dataclass(frozen=True)
class AskConfig:
    """Budgets from ADR 0033, decision 6. `top_k` is RETRIEVAL_TOP_K."""

    deadline_seconds: float = 12.0
    top_k: int = 5
    state_timeout: float = 2.0
    embed_timeout: float = 5.0  # the 2 s slot wait and the 3 s embedding, together
    retrieve_timeout: float = 3.0
    metering_timeout: float = 2.0


def seconds_until_utc_midnight(now: datetime) -> int:
    """Whole seconds to the next 00:00 UTC, rounded up, at least 1: when the daily cap resets."""
    moment = now.astimezone(UTC)
    midnight = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((midnight - moment).total_seconds()))


class AskService:
    def __init__(
        self,
        *,
        limiter: Limiter,
        in_flight: InFlight,
        flag: MeteringFlag,
        store: Store,
        embedder: Embedder,
        providers: Providers,
        log_key: bytes,
        config: AskConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
        now_utc: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._limiter = limiter
        self._in_flight = in_flight
        self._flag = flag
        self._store = store
        self._embedder = embedder
        self._providers = providers
        self._log_key = log_key
        self._config = config or AskConfig()
        self._clock = clock
        self._now_utc = now_utc
        self._sleep = sleep

    async def ask(self, caller: Caller, question: str, request_id: str) -> AskResult:
        """Run the flow. Returns a result for every expected outcome; an unexpected exception is metered and raised."""
        tenant_ref = tenant_log_ref(self._log_key, caller.tenant_id)
        started = self._clock()

        def unavailable(stage: str, code: str, retry: int | None, reason: str) -> Unavailable:
            logger.warning(
                "ask.unavailable request_id=%s tenant=%s stage=%s reason=%s code=%s",
                request_id,
                tenant_ref,
                stage,
                reason,
                code,
            )
            return Unavailable(request_id, code, retry)

        # 1. Rate limit. Counted per attempt, before any work.
        try:
            wait = await self._limiter.admit(caller.tenant_id, caller.user_id)
        except LimiterUnavailable:
            return unavailable("rate_limit", "service_unavailable", 30, "valkey")
        if wait is not None:
            logger.info(
                "ask.limited request_id=%s tenant=%s code=rate_limited", request_id, tenant_ref
            )
            return Limited(request_id, "rate_limited", wait)

        # 2. A persistent metering failure for this tenant: refuse rather than spend unmetered.
        try:
            metering_failed = await self._flag.is_set(caller.tenant_id)
        except LimiterUnavailable:
            return unavailable("metering_flag", "service_unavailable", 30, "valkey")
        if metering_failed:
            return unavailable("metering_flag", "service_unavailable", 30, "metering_failed")

        # 3. The tenant's in-flight slot.
        try:
            granted = await self._in_flight.acquire(caller.tenant_id, request_id)
        except LimiterUnavailable:
            return unavailable("in_flight", "service_unavailable", 30, "valkey")
        if not granted:
            logger.info("ask.limited request_id=%s tenant=%s code=busy", request_id, tenant_ref)
            return Limited(request_id, "busy", BUSY_RETRY_SECONDS)

        try:
            return await self._answer(caller, question, request_id, tenant_ref, started)
        finally:
            await self._release(caller, request_id, tenant_ref)

    async def _release(self, caller: Caller, request_id: str, tenant_ref: str) -> None:
        """Free the in-flight slot, shielded from cancellation. A failure is logged and heals when the entry expires."""
        try:
            await asyncio.shield(
                asyncio.wait_for(
                    self._in_flight.release(caller.tenant_id, request_id), RELEASE_TIMEOUT_SECONDS
                )
            )
        except (LimiterUnavailable, TimeoutError) as exc:
            logger.warning(
                "ask.release_failed request_id=%s tenant=%s reason=%s",
                request_id,
                tenant_ref,
                type(exc).__name__,
            )

    async def _stage[T](self, work: Awaitable[T], limit: float, deadline: float) -> T:
        """Await `work` for at most `limit` seconds or the time left, whichever is smaller. TimeoutError when out."""
        remaining = deadline - self._clock()
        if remaining <= 0:
            close = getattr(work, "close", None)
            if close is not None:
                close()
            raise TimeoutError
        async with asyncio.timeout(min(limit, remaining)):
            return await work

    async def _answer(
        self, caller: Caller, question: str, request_id: str, tenant_ref: str, started: float
    ) -> AskResult:
        config = self._config
        deadline = started + config.deadline_seconds
        metered = False

        def latency_ms() -> int:
            return min(MAX_LATENCY_MS, max(0, int((self._clock() - started) * 1000)))

        async def meter(record: UsageRecord) -> None:
            nonlocal metered
            metered = True
            await self._record(caller, record, request_id, tenant_ref)

        def empty(
            outcome: str, provider: str | None = None, model: str | None = None
        ) -> UsageRecord:
            return UsageRecord(provider, model, outcome, 0, 0, 0, 0, 0, latency_ms(), ())

        # 4. Settings and today's spend, in one short transaction. No row to read means no answers.
        try:
            state: TenantState = await self._stage(
                self._store.read_state(caller), config.state_timeout, deadline
            )
        except (StoreUnavailable, TimeoutError) as exc:
            logger.warning(
                "ask.unavailable request_id=%s tenant=%s stage=state reason=%s",
                request_id,
                tenant_ref,
                type(exc).__name__,
            )
            return Unavailable(request_id, "service_unavailable", 30)

        try:
            settings = state.settings
            if settings is None or not settings.answer_providers:
                await meter(empty("tenant_not_configured"))
                logger.warning(
                    "ask.unavailable request_id=%s tenant=%s stage=settings reason=tenant_not_configured",
                    request_id,
                    tenant_ref,
                )
                return Unavailable(request_id, "not_configured", None)
            if state.spent_micro_usd >= settings.daily_cost_cap_micro_usd:
                wait = seconds_until_utc_midnight(self._now_utc())
                logger.info(
                    "ask.limited request_id=%s tenant=%s code=daily_limit", request_id, tenant_ref
                )
                return Limited(request_id, "daily_limit", wait)

            # 5. The provider: the first on the list, if the policy allows it and a key is configured.
            name = settings.answer_providers[0]
            if not policy.is_allowed(name, settings.answer_providers, settings.data_class):
                await meter(empty("provider_disallowed", name))
                return self._stopped(request_id, tenant_ref, "policy", "provider_disallowed")
            model = self._providers.get(name)
            if model is None:
                await meter(empty("service_error", name))
                return self._stopped(request_id, tenant_ref, "provider", "provider_key_missing")
            try:
                cost_micro_usd(model.model, Usage(0, 0, None))
            except UnknownModelPrice:
                # A model with no list price cannot be capped, so it is not called.
                await meter(empty("service_error", name, model.model))
                return self._stopped(request_id, tenant_ref, "provider", "model_price_unknown")

            # 6. Embed.
            try:
                vector = await self._stage(
                    self._embedder.embed(question), config.embed_timeout, deadline
                )
            except QueryEmbedderNotReady:
                await meter(empty("service_error", name, model.model))
                return self._stopped(
                    request_id,
                    tenant_ref,
                    "embed",
                    "embedder_not_ready",
                    "service_unavailable",
                    EMBEDDER_LOADING_RETRY_SECONDS,
                )
            except QueryEmbedderBusy:
                await meter(empty("service_error", name, model.model))
                return self._stopped(
                    request_id, tenant_ref, "embed", "embedder_busy", "try_again", 2
                )
            except (QueryEmbedderTimeout, TimeoutError):
                await meter(empty("timeout", name, model.model))
                return self._stopped(request_id, tenant_ref, "embed", "timeout", "try_again", 5)

            # 7. Retrieve.
            try:
                chunks = await self._stage(
                    self._store.retrieve(caller, question, vector, config.top_k),
                    config.retrieve_timeout,
                    deadline,
                )
            except StoreUnavailable:
                await meter(empty("service_error", name, model.model))
                return self._stopped(request_id, tenant_ref, "retrieve", "store_unavailable")
            except TimeoutError:
                await meter(empty("timeout", name, model.model))
                return self._stopped(request_id, tenant_ref, "retrieve", "timeout", "try_again", 5)

            # 8. Answer.
            try:
                outcome = await answer_question(
                    model, question, chunks, deadline=deadline, clock=self._clock, sleep=self._sleep
                )
            except AnswerServiceError as error:
                return await self._provider_failure(
                    error, model, len(chunks), latency_ms(), request_id, tenant_ref, meter
                )

            # 9. Meter, then answer.
            record = self._usage_record(outcome, len(chunks), latency_ms())
            await meter(record)
            logger.info(
                "ask.done request_id=%s tenant=%s outcome=%s attempts=%d retrieved=%d "
                "input_tokens=%d output_tokens=%d cost_micro_usd=%d ms=%d",
                request_id,
                tenant_ref,
                record.outcome,
                record.attempts,
                record.retrieved_count,
                record.input_tokens,
                record.output_tokens,
                record.cost_micro_usd,
                record.latency_ms,
            )
            if isinstance(outcome.result, GroundedAnswer):
                return Answered(request_id, outcome.result)
            return Refused(request_id, outcome.result)
        except asyncio.CancelledError:
            # The client went away. The model may already have been paid for, so the request is still metered.
            if not metered:
                await asyncio.shield(
                    asyncio.ensure_future(
                        self._record(caller, empty("service_error"), request_id, tenant_ref)
                    )
                )
            raise
        except Exception as exc:
            # Anything unexpected: logged by class only, metered once, then the error goes up.
            logger.error(
                "ask.failed request_id=%s tenant=%s reason=%s",
                request_id,
                tenant_ref,
                type(exc).__name__,
            )
            if not metered:
                await self._record(caller, empty("service_error"), request_id, tenant_ref)
            raise

    def _stopped(
        self,
        request_id: str,
        tenant_ref: str,
        stage: str,
        reason: str,
        code: str = "service_unavailable",
        retry: int | None = 30,
    ) -> Unavailable:
        logger.warning(
            "ask.unavailable request_id=%s tenant=%s stage=%s reason=%s code=%s",
            request_id,
            tenant_ref,
            stage,
            reason,
            code,
        )
        return Unavailable(request_id, code, retry)

    def _usage_record(self, outcome: AnswerOutcome, retrieved: int, latency_ms: int) -> UsageRecord:
        if retrieved == 0:
            label = "no_sources"
        elif isinstance(outcome.result, GroundedAnswer):
            label = "answered"
        else:
            label = outcome_for_refusal(outcome.result.kind)
        return UsageRecord(
            provider=outcome.provider,
            model=outcome.model,
            outcome=label,
            attempts=outcome.attempts,
            retrieved_count=retrieved,
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
            cost_micro_usd=cost_micro_usd(outcome.model, outcome.usage),
            latency_ms=latency_ms,
            invalid_codes=outcome.invalid,
        )

    async def _provider_failure(
        self,
        error: AnswerServiceError,
        model: AnswerModel,
        retrieved: int,
        latency_ms: int,
        request_id: str,
        tenant_ref: str,
        meter: Callable[[UsageRecord], Awaitable[None]],
    ) -> Unavailable:
        cause = error.__cause__
        retry: int | None = 5
        if isinstance(error, AnswerDeadlineExceeded) or isinstance(cause, ProviderTimeout):
            outcome, reason = "timeout", "timeout"
        elif isinstance(cause, ProviderRateLimited):
            outcome, reason = "provider_rate_limited", "provider_rate_limited"
            hint = cause.retry_after_seconds
            retry = min(MAX_RETRY_AFTER_SECONDS, max(1, math.ceil(hint))) if hint else 5
        else:
            outcome, reason = "provider_error", type(cause).__name__ if cause else "provider_error"
        await meter(
            UsageRecord(
                provider=model.provider,
                model=model.model,
                outcome=outcome,
                attempts=error.attempts,
                retrieved_count=retrieved,
                input_tokens=error.usage.input_tokens,
                output_tokens=error.usage.output_tokens,
                cost_micro_usd=cost_micro_usd(model.model, error.usage),
                latency_ms=latency_ms,
                invalid_codes=(),
            )
        )
        return self._stopped(request_id, tenant_ref, "answer", reason, "try_again", retry)

    async def _record(
        self, caller: Caller, record: UsageRecord, request_id: str, tenant_ref: str
    ) -> None:
        """
        Write the metering row: shielded, with a timeout, tried twice. If both fail, set the tenant's flag.

        Does not raise for a database failure: the answer was produced and is returned. The flag is what keeps the
        failure from becoming silent (decision D5).
        """
        for attempt in (1, 2):
            try:
                await asyncio.shield(
                    asyncio.wait_for(
                        self._store.record(caller, record), self._config.metering_timeout
                    )
                )
                return
            except (
                Exception
            ) as exc:  # any failure to write must lead to the flag, whatever its type
                logger.error(
                    "ask.metering_insert_failed request_id=%s tenant=%s attempt=%d reason=%s",
                    request_id,
                    tenant_ref,
                    attempt,
                    type(exc).__name__,
                )
        logger.error("ask.metering_failed request_id=%s tenant=%s", request_id, tenant_ref)
        try:
            await self._flag.set(caller.tenant_id)
        except LimiterUnavailable:
            logger.error(
                "ask.metering_flag_not_set request_id=%s tenant=%s", request_id, tenant_ref
            )


def new_request_id() -> str:
    """A random id for one request: it correlates a log line with a response and carries no content."""
    return str(uuid.uuid4())

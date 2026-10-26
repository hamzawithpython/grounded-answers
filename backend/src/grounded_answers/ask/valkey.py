"""
The Valkey parts of POST /ask (ADR 0033, decision 3): the per-user and per-tenant rate limit, the per-tenant in-flight
set, and the metering-failure flag.

All of them fail closed. A Valkey error or a timeout raises LimiterUnavailable, and the flow answers 503 without
calling a model. Every call also has its own short timeout on top of the client's socket timeout. Keys hold an HMAC of
the ids (the rate-limit key, ADR 0014), never a raw id.

Rate limit: one-minute fixed windows, counted per attempt before any work, as the upload limiter does (ADR 0019).

In-flight set: a sorted set of request ids scored by when each entry expires. Acquire removes expired entries, refuses
when the set is full, and adds the request, in one script. Release removes the request. Because each entry expires on
its own, a request whose process was killed cannot hold a slot longer than the hold time, and other traffic cannot
keep the leak alive (a counter with a refreshed expiry could).

Metering flag: set when a metering insert has failed twice, for a short time. While it is set the tenant's /ask answers
503, so a persistent metering failure (a missing grant, a full disk) cannot leave the daily cap reading zero and let
spending go on unmetered.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from grounded_answers.rate_limit import (
    INCR_WITH_EXPIRY_LUA,
    LimiterUnavailable,
    digest,
    retry_seconds,
    window_end_ms,
    window_index,
)

logger = logging.getLogger(__name__)

RATE_PREFIX: Final = "grounded:ask:rate"
INFLIGHT_PREFIX: Final = "grounded:ask:inflight"
FLAG_PREFIX: Final = "grounded:ask:meteringfail"
RATE_WINDOW_MS: Final = 60 * 1000
CALL_TIMEOUT_SECONDS: Final = 0.5
FLAG_TTL_SECONDS: Final = 300

Clock = Callable[[], int]

_ACQUIRE_LUA: Final = """
local now = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], tonumber(ARGV[3]), ARGV[4])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]) - now + 1000)
return 1
"""


async def _guarded[T](operation: str, call: Awaitable[T]) -> T:
    """Await one Valkey call under the call timeout. Any Valkey error or timeout becomes LimiterUnavailable."""
    try:
        async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
            return await call
    except (RedisError, TimeoutError, OSError) as exc:
        logger.warning(
            "ask.valkey_unavailable operation=%s reason=%s", operation, type(exc).__name__
        )
        raise LimiterUnavailable() from exc


class AskLimiter:
    """Counts questions per tenant and per user in one-minute windows."""

    def __init__(
        self,
        cache: Redis,
        hmac_key: bytes,
        clock: Clock,
        *,
        tenant_limit: int,
        user_limit: int,
    ) -> None:
        self.cache = cache
        self.hmac_key = hmac_key
        self.clock = clock
        self.tenant_limit = tenant_limit
        self.user_limit = user_limit
        self._incr = cache.register_script(INCR_WITH_EXPIRY_LUA)

    async def admit(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> int | None:
        """Count one attempt. None when allowed, else the seconds to wait. Raises LimiterUnavailable."""
        now = self.clock()
        window = window_index(now, RATE_WINDOW_MS)
        tenant_key = (
            f"{RATE_PREFIX}:tenant:{digest(self.hmac_key, 'ask-tenant', str(tenant_id))}:{window}"
        )
        user_key = f"{RATE_PREFIX}:user:{digest(self.hmac_key, 'ask-user', str(user_id))}:{window}"
        tenant_count = int(
            await _guarded("rate_tenant", self._incr(keys=[tenant_key], args=[RATE_WINDOW_MS]))
        )
        user_count = int(
            await _guarded("rate_user", self._incr(keys=[user_key], args=[RATE_WINDOW_MS]))
        )
        if tenant_count > self.tenant_limit or user_count > self.user_limit:
            return retry_seconds(window_end_ms(now, RATE_WINDOW_MS) - now)
        return None


class InFlightSet:
    """The tenant's requests in progress, each with its own expiry."""

    def __init__(
        self, cache: Redis, hmac_key: bytes, clock: Clock, *, limit: int, hold_seconds: float
    ) -> None:
        self.cache = cache
        self.hmac_key = hmac_key
        self.clock = clock
        self.limit = limit
        self.hold_ms = int(hold_seconds * 1000)
        self._acquire = cache.register_script(_ACQUIRE_LUA)

    def _key(self, tenant_id: uuid.UUID) -> str:
        return f"{INFLIGHT_PREFIX}:{digest(self.hmac_key, 'ask-inflight', str(tenant_id))}"

    async def acquire(self, tenant_id: uuid.UUID, request_id: str) -> bool:
        now = self.clock()
        granted = await _guarded(
            "inflight_acquire",
            self._acquire(
                keys=[self._key(tenant_id)],
                args=[now, self.limit, now + self.hold_ms, request_id],
            ),
        )
        return int(granted) == 1

    async def release(self, tenant_id: uuid.UUID, request_id: str) -> None:
        await _guarded("inflight_release", self.cache.zrem(self._key(tenant_id), request_id))


class MeteringFlagStore:
    """A short-lived flag per tenant: set after a metering insert failed twice."""

    def __init__(
        self, cache: Redis, hmac_key: bytes, *, ttl_seconds: int = FLAG_TTL_SECONDS
    ) -> None:
        self.cache = cache
        self.hmac_key = hmac_key
        self.ttl_seconds = ttl_seconds

    def _key(self, tenant_id: uuid.UUID) -> str:
        return f"{FLAG_PREFIX}:{digest(self.hmac_key, 'ask-meteringfail', str(tenant_id))}"

    async def is_set(self, tenant_id: uuid.UUID) -> bool:
        found = await _guarded("flag_read", self.cache.exists(self._key(tenant_id)))
        return int(found) > 0

    async def set(self, tenant_id: uuid.UUID) -> None:
        await _guarded("flag_set", self.cache.set(self._key(tenant_id), "1", ex=self.ttl_seconds))

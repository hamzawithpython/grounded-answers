"""
Upload rate limit: per tenant and per user, in Valkey (ADR 0019, decision D8; ADR 0014 pattern).

Each attempt counts, including one that is later refused for its type or size: the limit is on
attempts, so a client cannot probe the rules for free. Keys hold an HMAC of the ids, not the ids,
and expire with their window. If Valkey is unavailable the upload is refused (fail closed).
"""

import logging
import uuid
from collections.abc import Callable
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

PREFIX: Final = "grounded:upload"
TENANT_LIMIT: Final = 60
USER_LIMIT: Final = 20
WINDOW_MS: Final = 60 * 60 * 1000  # one hour

Clock = Callable[[], int]


class UploadLimiter:
    """Counts upload attempts per tenant and per user, in one-hour windows."""

    def __init__(
        self,
        cache: Redis,
        hmac_key: bytes,
        clock: Clock,
        *,
        tenant_limit: int = TENANT_LIMIT,
        user_limit: int = USER_LIMIT,
    ) -> None:
        self.cache = cache
        self.hmac_key = hmac_key
        self.clock = clock
        self.tenant_limit = tenant_limit
        self.user_limit = user_limit
        self._incr = cache.register_script(INCR_WITH_EXPIRY_LUA)

    async def admit(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> int | None:
        """
        Count one attempt. Return None when it is allowed, or the seconds to wait when it is not.

        Raises LimiterUnavailable when Valkey does not answer. The caller refuses the upload.
        """
        now = self.clock()
        window = window_index(now, WINDOW_MS)
        tenant_key = f"{PREFIX}:tenant:{digest(self.hmac_key, 'tenant', str(tenant_id))}:{window}"
        user_key = f"{PREFIX}:user:{digest(self.hmac_key, 'user', str(user_id))}:{window}"
        try:
            tenant_count = int(await self._incr(keys=[tenant_key], args=[WINDOW_MS]))
            user_count = int(await self._incr(keys=[user_key], args=[WINDOW_MS]))
        except RedisError as exc:
            logger.warning("upload.rate_limiter_unavailable reason=%s", type(exc).__name__)
            raise LimiterUnavailable() from exc
        if tenant_count > self.tenant_limit or user_count > self.user_limit:
            return retry_seconds(window_end_ms(now, WINDOW_MS) - now)
        return None

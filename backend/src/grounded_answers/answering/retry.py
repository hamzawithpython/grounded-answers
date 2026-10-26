"""
Retries live outside the adapters (ADR 0031, decision 4): an adapter makes one call and raises a typed error.

Retried: a timeout, an outage (network, 5xx) and a rate limit. A rate limit waits for the provider's hint. A hint longer
than `max_wait_seconds` means a daily quota, not a busy minute, so it is raised at once instead of sleeping for hours.
Not retried: a rejected request, a refusal and an invalid output, because the same request would fail the same way.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from grounded_answers.answering.types import (
    AnswerModel,
    AnswerRequest,
    AnswerResult,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS: Final = 4
DEFAULT_MAX_WAIT_SECONDS: Final = 90.0
_BACKOFF_SECONDS: Final = (1.0, 2.0, 4.0)


async def answer_with_retries(
    model: AnswerModel,
    request: AnswerRequest,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AnswerResult:
    for attempt in range(1, attempts + 1):
        try:
            return await model.answer(request)
        except ProviderRateLimited as error:
            wait = error.retry_after_seconds
            if wait is None:
                wait = _BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)]
            if attempt == attempts or wait > max_wait_seconds:
                raise
        except (ProviderTimeout, ProviderUnavailable):
            if attempt == attempts:
                raise
            wait = _BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)]
        logger.warning(
            "answer call retried",
            extra={"provider": model.provider, "attempt": attempt, "wait_seconds": wait},
        )
        await sleep(wait)
    raise ValueError("attempts must be at least 1")

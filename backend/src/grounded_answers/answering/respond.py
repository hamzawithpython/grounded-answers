"""
From a question and its retrieved chunks to a checked answer or a refusal (ADR 0032).

    no chunks -> refusal "not_in_documents", the model is not called
    ask the model -> parse -> the model refused? -> refusal with our own wording
                              -> validate the citations and numbers (validate.py)
    invalid output or invalid answer -> ask once more with a fixed corrective note -> still invalid -> "could_not_verify"
    the provider failed (timeout, outage, rate limit, bad key) -> AnswerServiceError (an error, not a refusal)
    the provider declined (a safety block) -> refusal "declined"

Broken output is never returned. A score threshold plays no part: Step 1 showed that answerable and unanswerable
questions overlap in score.

The prompt carries only the question and the chunk text (prompt.py). The identifiers, titles, versions and clause
labels stay on our side and are attached to the citations afterwards.

Logged: provider, model, outcome, attempts, the codes of invalid outputs and token counts. Never the question, a source,
an answer or a quote.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

from grounded_answers.answering.clauses import ClauseLabel
from grounded_answers.answering.messages import REFUSAL_MESSAGES
from grounded_answers.answering.retry import answer_with_retries
from grounded_answers.answering.schema import NO_REFUSAL
from grounded_answers.answering.types import (
    AnswerModel,
    AnswerModelError,
    AnswerRequest,
    AnswerResult,
    InvalidOutput,
    ProviderRefused,
    Usage,
    sources_from_chunks,
)
from grounded_answers.answering.validate import Invalid, validate_answer
from grounded_answers.retrieval import RetrievedChunk

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2

# Used only when a caller passes a deadline (POST /ask, ADR 0033 decision 6).
ATTEMPT_TIMEOUT_SECONDS = 8.0  # one model call, at most
CORRECTIVE_MIN_SECONDS = 4.0  # no corrective retry with less time than this left
NETWORK_ATTEMPTS = 2  # calls per model attempt (the retry helper's default is 4)
RETRY_WAIT_MARGIN_SECONDS = (
    2.0  # a provider's retry hint must leave this much time for the call that follows
)


class AnswerServiceError(AnswerModelError):
    """
    The provider failed after the network retries. `__cause__` is the provider error (its type, never its text).

    `usage` is what the attempts before the failure used, and `attempts` how many were made, so a caller that meters
    cost can count them (ADR 0033).
    """

    def __init__(self, message: str, *, usage: Usage | None = None, attempts: int = 0) -> None:
        super().__init__(message)
        self.usage = usage if usage is not None else Usage(0, 0, None)
        self.attempts = attempts


class AnswerDeadlineExceeded(AnswerServiceError):
    """The caller's deadline passed before an answer was ready."""


@dataclass(frozen=True)
class CitedSource:
    ref: int
    chunk: RetrievedChunk
    quote: str
    location: ClauseLabel


@dataclass(frozen=True)
class GroundedAnswer:
    text: str
    citations: tuple[CitedSource, ...]


@dataclass(frozen=True)
class Refusal:
    # not_in_documents, needs_clarification, conflicting_sources, could_not_verify or declined.
    kind: str
    message: str


@dataclass(frozen=True)
class AnswerOutcome:
    result: GroundedAnswer | Refusal
    attempts: int
    # Codes of the invalid outputs seen, in order (validate.py codes, or "invalid_output" for a parse failure).
    invalid: tuple[str, ...]
    usage: Usage
    latency_ms: float
    provider: str
    model: str
    quota: Mapping[str, str] = field(default_factory=dict)

    @property
    def review_signals(self) -> tuple[str, ...]:
        """Why an admin may want to look at this answer (replaces a model-reported confidence, SPEC 6.3)."""
        signals = list(self.invalid)
        if self.attempts > 1:
            signals.insert(0, "retried")
        if isinstance(self.result, Refusal) and self.result.kind == "could_not_verify":
            signals.append("could_not_verify")
        return tuple(signals)


def _refusal(kind: str) -> Refusal:
    return Refusal(kind, REFUSAL_MESSAGES[kind])


def _add(total: Usage, more: Usage) -> Usage:
    reasoning = None
    if total.reasoning_tokens is not None or more.reasoning_tokens is not None:
        reasoning = (total.reasoning_tokens or 0) + (more.reasoning_tokens or 0)
    return Usage(
        total.input_tokens + more.input_tokens, total.output_tokens + more.output_tokens, reasoning
    )


async def answer_question(
    model: AnswerModel,
    question: str,
    chunks: Sequence[RetrievedChunk],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AnswerOutcome:
    """
    Ask the model about `question` using `chunks` (best first). Raises AnswerServiceError if the provider fails.

    `deadline` (a `clock()` value) is for callers with a time budget. With it: each model call gets at most
    ATTEMPT_TIMEOUT_SECONDS or the time left, whichever is smaller; a call is retried on a network error at most
    NETWORK_ATTEMPTS times and never waits longer than the time left; the corrective retry after an invalid answer
    is skipped when under CORRECTIVE_MIN_SECONDS remain, which ends in the same fixed refusal as a second invalid
    answer; and when the time is gone AnswerDeadlineExceeded is raised with the usage so far. Without it nothing
    changes: the evaluation runs use that.
    """
    usage = Usage(0, 0, None)
    latency = 0.0
    invalid: list[str] = []
    quota: Mapping[str, str] = {}

    def outcome(result: GroundedAnswer | Refusal, attempts: int) -> AnswerOutcome:
        done = AnswerOutcome(
            result, attempts, tuple(invalid), usage, latency, model.provider, model.model, quota
        )
        logger.info(
            "answer outcome",
            extra={
                "answer_provider": model.provider,
                "answer_model": model.model,
                "answer_outcome": "answered" if isinstance(result, GroundedAnswer) else result.kind,
                "answer_attempts": attempts,
                "answer_invalid_codes": list(invalid),
                "answer_input_tokens": usage.input_tokens,
                "answer_output_tokens": usage.output_tokens,
            },
        )
        return done

    if not chunks:
        return outcome(_refusal("not_in_documents"), 0)
    sources = sources_from_chunks(chunks)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        remaining: float | None = None
        timeout_seconds = AnswerRequest.timeout_seconds
        if deadline is not None:
            remaining = deadline - clock()
            if attempt > 1 and remaining < CORRECTIVE_MIN_SECONDS:
                return outcome(_refusal("could_not_verify"), attempt - 1)
            if remaining <= 0:
                raise AnswerDeadlineExceeded("deadline", usage=usage, attempts=attempt - 1)
            timeout_seconds = min(ATTEMPT_TIMEOUT_SECONDS, remaining)
        request = AnswerRequest(
            question, sources, timeout_seconds=timeout_seconds, corrective=attempt > 1
        )
        result: AnswerResult | None = None
        try:
            if remaining is None:
                result = await answer_with_retries(model, request, sleep=sleep)
            else:
                async with asyncio.timeout(remaining):
                    result = await answer_with_retries(
                        model,
                        request,
                        sleep=sleep,
                        attempts=NETWORK_ATTEMPTS,
                        max_wait_seconds=max(0.0, remaining - RETRY_WAIT_MARGIN_SECONDS),
                    )
        except TimeoutError:
            raise AnswerDeadlineExceeded("deadline", usage=usage, attempts=attempt) from None
        except InvalidOutput:
            invalid.append("invalid_output")
        except ProviderRefused:
            return outcome(_refusal("declined"), attempt)
        except AnswerModelError as error:
            raise AnswerServiceError(type(error).__name__, usage=usage, attempts=attempt) from error
        if result is not None:
            usage = _add(usage, result.usage)
            latency += result.latency_ms
            quota = result.quota or quota
            if result.answer.refused:
                reason = result.answer.refusal_reason
                kind = "not_in_documents" if reason == NO_REFUSAL else reason
                return outcome(_refusal(kind), attempt)
            checked = validate_answer(result.answer, chunks)
            if not isinstance(checked, Invalid):
                cited = tuple(
                    CitedSource(c.ref, chunks[c.chunk_index], c.quote, c.location)
                    for c in checked.citations
                )
                return outcome(GroundedAnswer(result.answer.answer.strip(), cited), attempt)
            invalid.append(checked.code)
    return outcome(_refusal("could_not_verify"), MAX_ATTEMPTS)

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


class AnswerServiceError(AnswerModelError):
    """The provider failed after the network retries. `__cause__` is the provider error (its type, never its text)."""


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
) -> AnswerOutcome:
    """Ask the model about `question` using `chunks` (best first). Raises AnswerServiceError if the provider fails."""
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
        request = AnswerRequest(question, sources, corrective=attempt > 1)
        result: AnswerResult | None = None
        try:
            result = await answer_with_retries(model, request, sleep=sleep)
        except InvalidOutput:
            invalid.append("invalid_output")
        except ProviderRefused:
            return outcome(_refusal("declined"), attempt)
        except AnswerModelError as error:
            raise AnswerServiceError(type(error).__name__) from error
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

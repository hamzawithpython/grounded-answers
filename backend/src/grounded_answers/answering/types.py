"""
The provider-neutral answer interface (ADR 0031, decision 4).

The rest of the application sees only `AnswerModel`, `AnswerRequest` and `AnswerResult`. An adapter translates one
call to one provider. Retries, citation validation, the refusal rule and cost caps live outside the adapters.

A request holds the question and the text of the sources, nothing else. There is no field for a user, an email, a
tenant or a document, so none of them can reach a prompt (decision 5; the tests check the bytes sent).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from grounded_answers.retrieval import RetrievedChunk


@dataclass(frozen=True)
class SourceChunk:
    """One source as the model sees it: a number local to this request and the chunk text."""

    ref: int
    text: str


@dataclass(frozen=True)
class AnswerRequest:
    question: str
    sources: tuple[SourceChunk, ...]
    # Reasoning tokens count against this limit on reasoning models, so it is not small.
    max_output_tokens: int = 1000
    timeout_seconds: float = 45.0
    # The one retry after invalid output adds a fixed note to the prompt (ADR 0032, decision 5). It never carries
    # the previous output or the error.
    corrective: bool = False


def sources_from_chunks(chunks: Sequence[RetrievedChunk]) -> tuple[SourceChunk, ...]:
    """Number the chunks 1..n in retrieval order. Only the text is kept: no ids, no title, no version."""
    return tuple(SourceChunk(ref, chunk.body) for ref, chunk in enumerate(chunks, start=1))


@dataclass(frozen=True)
class Citation:
    """What the model cites: the number of a source and a quote that must appear in it (checked in validate.py)."""

    ref: int
    quote: str


@dataclass(frozen=True)
class ModelAnswer:
    """The answer contract as the model returns it (ADR 0032). A refusal has an empty answer and no citations."""

    refused: bool
    refusal_reason: str
    answer: str
    citations: tuple[Citation, ...]


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    # Every generated token that is billed, reasoning included.
    output_tokens: int
    # Of output_tokens, how many were reasoning, when the provider says. None means "not reported".
    reasoning_tokens: int | None


@dataclass(frozen=True)
class AnswerResult:
    answer: ModelAnswer
    usage: Usage
    latency_ms: float
    provider: str
    model: str
    # The provider's rate-limit headers (x-ratelimit-*), if any. Numbers and reset times, nothing secret.
    quota: Mapping[str, str] = field(default_factory=dict)


class AnswerModelError(Exception):
    """Base of every error an adapter raises. The message never holds a key, a prompt or an answer."""


class ProviderTimeout(AnswerModelError):
    """No response within the request's timeout."""


class ProviderUnavailable(AnswerModelError):
    """A network failure or a 5xx. Worth retrying."""


class ProviderRateLimited(AnswerModelError):
    """HTTP 429. `retry_after_seconds` is the provider's hint, if it sent one."""

    def __init__(self, message: str, retry_after_seconds: float | None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderRejected(AnswerModelError):
    """Any other HTTP error (bad key, unknown model, invalid request). Retrying will not help."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class ProviderRefused(AnswerModelError):
    """The provider declined to answer (a safety block or a refusal stop). Not an answer of ours."""


class InvalidOutput(AnswerModelError):
    """The output was cut off or does not match the answer schema."""


class AnswerModel(Protocol):
    provider: str
    model: str

    async def answer(self, request: AnswerRequest) -> AnswerResult: ...

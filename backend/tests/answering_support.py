"""Fake HTTP for the answer-model adapters: a mock transport that records requests and replays canned responses."""

import json
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from grounded_answers.answering import (
    AnswerRequest,
    AnswerResult,
    SourceChunk,
    Usage,
)
from grounded_answers.answering.schema import parse_answer
from grounded_answers.retrieval import RetrievedChunk


def answer_json(
    *,
    refused: bool = False,
    reason: str = "none",
    answer: str = "Notice is thirty days.",
    citations: list[dict[str, Any]] | None = None,
) -> str:
    """A model reply under the answer contract (ADR 0032)."""
    if citations is None:
        citations = [] if refused else [{"ref": 1, "quote": "Notice is thirty days."}]
    return json.dumps(
        {"refused": refused, "refusal_reason": reason, "answer": answer, "citations": citations}
    )


ANSWER_JSON = answer_json()

REQUEST = AnswerRequest(
    "How many days of notice?",
    (SourceChunk(1, "Notice is thirty days."), SourceChunk(2, "Leave is paid.")),
)


def groq_response(
    text: str = ANSWER_JSON, *, finish: str = "stop", reasoning: int | None = 40
) -> dict[str, Any]:
    usage: dict[str, Any] = {"prompt_tokens": 1100, "completion_tokens": 90, "total_tokens": 1190}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}], "usage": usage}


def gemini_response(
    text: str = ANSWER_JSON, *, finish: str = "STOP", thoughts: int | None = None
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "promptTokenCount": 1000,
        "candidatesTokenCount": 60,
        "totalTokenCount": 1060,
    }
    if thoughts is not None:
        usage["thoughtsTokenCount"] = thoughts
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}],
        "usageMetadata": usage,
    }


def anthropic_response(text: str = ANSWER_JSON, *, stop: str = "end_turn") -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": {"input_tokens": 1050, "output_tokens": 70},
    }


class Wire:
    """An AsyncClient on a mock transport. `requests` holds every request sent, in order."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return respond(request)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @property
    def last_body(self) -> dict[str, Any]:
        body = json.loads(self.requests[-1].content)
        assert isinstance(body, dict)
        return body


def replying(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> Wire:
    return Wire(lambda _request: httpx.Response(status, json=body, headers=headers))


def make_chunk(
    body: str, *, section_number: str | None = "4", title: str = "Policy", position: int = 0
) -> RetrievedChunk:
    return RetrievedChunk(
        version_id=uuid.uuid4(),
        position=position,
        document_id=uuid.uuid4(),
        title=title,
        version_no=1,
        section_number=section_number,
        heading="Heading",
        clause_labels=(),
        body=body,
        score=0.03,
        vector_rank=1,
        keyword_rank=1,
    )


def result_of(text: str, *, tokens_in: int = 1000, tokens_out: int = 100) -> AnswerResult:
    """What an adapter returns for a model reply `text` (JSON under the contract)."""
    return AnswerResult(
        parse_answer(text),
        Usage(tokens_in, tokens_out, 30),
        latency_ms=500.0,
        provider="fake",
        model="openai/gpt-oss-120b",
        quota={},
    )


class Scripted:
    """A model that plays a list of results or errors, one per call, and records the requests it got."""

    provider = "fake"
    model = "openai/gpt-oss-120b"

    def __init__(self, script: Sequence[AnswerResult | Exception]) -> None:
        self.script = list(script)
        self.requests: list[AnswerRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def answer(self, request: AnswerRequest) -> AnswerResult:
        self.requests.append(request)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)

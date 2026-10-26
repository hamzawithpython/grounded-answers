"""Fake HTTP for the answer-model adapters: a mock transport that records requests and replays canned responses."""

import json
from collections.abc import Callable
from typing import Any

import httpx

from grounded_answers.answering import AnswerRequest, SourceChunk

ANSWER_JSON = json.dumps({"answered": True, "answer": "Thirty days.", "citations": [1]})

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

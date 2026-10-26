"""
Gemini adapter (ADR 0031): `gemini-3.5-flash-lite` through generateContent with a response schema. Free tier is for
fictional data only (Google uses free-tier prompts and responses to improve its products, and human reviewers may read them).

The key goes in the `x-goog-api-key` header, never in the URL (Google's reference examples use `?key=`, which
would put the key in every logged or reported URL).

Gemini's schema is an OpenAPI subset and has no `additionalProperties`, so it is removed from the copy sent. If the model
does not accept a response schema, Google answers 400 and this adapter raises ProviderRejected. It never falls
back to parsing free text.
"""

from typing import Any, Final

import httpx
from pydantic import SecretStr

from grounded_answers.answering.prompt import build_prompt
from grounded_answers.answering.schema import ANSWER_SCHEMA, parse_answer
from grounded_answers.answering.transport import post_json
from grounded_answers.answering.types import (
    AnswerRequest,
    AnswerResult,
    InvalidOutput,
    ProviderRefused,
    Usage,
)

# gemini-2.5-flash-lite answers 404 for accounts that have not used it before (Step 3b).
DEFAULT_MODEL: Final = "gemini-3.5-flash-lite"
_BASE: Final = "https://generativelanguage.googleapis.com/v1beta/models"
# A candidate that stopped for one of these has no usable answer of ours.
_REFUSED: Final = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"})


def gemini_schema() -> dict[str, Any]:
    return {k: v for k, v in ANSWER_SCHEMA.items() if k != "additionalProperties"}


class GeminiAnswerModel:
    provider = "gemini"

    def __init__(
        self, api_key: SecretStr, client: httpx.AsyncClient, *, model: str = DEFAULT_MODEL
    ) -> None:
        self._api_key = api_key
        self._client = client
        self.model = model

    def __repr__(self) -> str:
        return f"GeminiAnswerModel(model={self.model!r})"

    async def answer(self, request: AnswerRequest) -> AnswerResult:
        prompt = build_prompt(request)
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": prompt.system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt.user}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": request.max_output_tokens,
                "responseMimeType": "application/json",
                "responseSchema": gemini_schema(),
            },
        }
        data, latency_ms, quota = await post_json(
            self._client,
            provider=self.provider,
            url=f"{_BASE}/{self.model}:generateContent",
            headers={"x-goog-api-key": self._api_key.get_secret_value()},
            body=body,
            timeout_seconds=request.timeout_seconds,
            secrets=(self._api_key,),
        )
        if data.get("promptFeedback", {}).get("blockReason"):
            raise ProviderRefused("gemini: the prompt was blocked")
        try:
            candidate = data["candidates"][0]
            usage = data["usageMetadata"]
            input_tokens = int(usage["promptTokenCount"])
            candidates_tokens = int(usage.get("candidatesTokenCount", 0))
            thoughts = int(usage.get("thoughtsTokenCount", 0))
        except (KeyError, IndexError, TypeError, ValueError):
            raise InvalidOutput("gemini: the response has an unexpected shape") from None
        finish = candidate.get("finishReason")
        if finish in _REFUSED:
            raise ProviderRefused(f"gemini: the answer was blocked ({finish})")
        if finish == "MAX_TOKENS":
            raise InvalidOutput("gemini: the output was cut off")
        try:
            text = "".join(part["text"] for part in candidate["content"]["parts"])
        except (KeyError, TypeError):
            raise InvalidOutput("gemini: the candidate has no text") from None
        return AnswerResult(
            answer=parse_answer(text),
            # Thinking tokens are billed as output but are not in candidatesTokenCount.
            usage=Usage(input_tokens, candidates_tokens + thoughts, thoughts),
            latency_ms=latency_ms,
            provider=self.provider,
            model=self.model,
            quota=quota,
        )

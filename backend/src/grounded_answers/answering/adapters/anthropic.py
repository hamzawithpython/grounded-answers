"""
Anthropic adapter (ADR 0031): Claude Haiku 5.5 through the Messages API with JSON-schema output.

Not tested against the live API during development (no paid account). It is tested offline with fake HTTP responses,
so it is ready for a client who brings their own paid, no-training key (BYOK). The first live call belongs to that
deployment's acceptance test.
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

URL: Final = "https://api.anthropic.com/v1/messages"
VERSION: Final = "2023-06-01"
DEFAULT_MODEL: Final = "claude-haiku-5-5"


class AnthropicAnswerModel:
    provider = "anthropic"

    def __init__(
        self, api_key: SecretStr, client: httpx.AsyncClient, *, model: str = DEFAULT_MODEL
    ) -> None:
        self._api_key = api_key
        self._client = client
        self.model = model

    def __repr__(self) -> str:
        return f"AnthropicAnswerModel(model={self.model!r})"

    async def answer(self, request: AnswerRequest) -> AnswerResult:
        prompt = build_prompt(request)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "system": prompt.system,
            "messages": [{"role": "user", "content": prompt.user}],
            "output_config": {"format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
        }
        data, latency_ms, quota = await post_json(
            self._client,
            provider=self.provider,
            url=URL,
            headers={"x-api-key": self._api_key.get_secret_value(), "anthropic-version": VERSION},
            body=body,
            timeout_seconds=request.timeout_seconds,
            secrets=(self._api_key,),
        )
        stop = data.get("stop_reason")
        if stop == "refusal":
            raise ProviderRefused("anthropic: the model refused")
        if stop == "max_tokens":
            raise InvalidOutput("anthropic: the output was cut off")
        try:
            text = "".join(
                block["text"] for block in data["content"] if block.get("type") == "text"
            )
            usage = data["usage"]
            input_tokens, output_tokens = int(usage["input_tokens"]), int(usage["output_tokens"])
        except (KeyError, TypeError, ValueError):
            raise InvalidOutput("anthropic: the response has an unexpected shape") from None
        return AnswerResult(
            answer=parse_answer(text),
            usage=Usage(input_tokens, output_tokens, None),
            latency_ms=latency_ms,
            provider=self.provider,
            model=self.model,
            quota=quota,
        )

"""
Groq adapter (ADR 0031): `openai/gpt-oss-120b` through the OpenAI-compatible chat completions endpoint, with strict
JSON-schema output and reasoning effort `low`. Demo tenant, fictional data only: Groq's training terms are unconfirmed.

The reasoning text is excluded from the response (`include_reasoning` false). Its tokens still count in
`completion_tokens`, and are therefore billed and counted against the free tier's daily limit.
"""

from typing import Any, Final

import httpx
from pydantic import SecretStr

from grounded_answers.answering.prompt import build_prompt
from grounded_answers.answering.schema import ANSWER_SCHEMA, ANSWER_SCHEMA_NAME, parse_answer
from grounded_answers.answering.transport import post_json
from grounded_answers.answering.types import (
    AnswerRequest,
    AnswerResult,
    InvalidOutput,
    Usage,
)

URL: Final = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL: Final = "openai/gpt-oss-120b"


class GroqAnswerModel:
    provider = "groq"

    def __init__(
        self,
        api_key: SecretStr,
        client: httpx.AsyncClient,
        *,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "low",
    ) -> None:
        self._api_key = api_key
        self._client = client
        self.model = model
        self._reasoning_effort = reasoning_effort

    def __repr__(self) -> str:
        return f"GroqAnswerModel(model={self.model!r})"

    async def answer(self, request: AnswerRequest) -> AnswerResult:
        prompt = build_prompt(request)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "temperature": 0,
            "max_completion_tokens": request.max_output_tokens,
            "reasoning_effort": self._reasoning_effort,
            "include_reasoning": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": ANSWER_SCHEMA_NAME,
                    "strict": True,
                    "schema": ANSWER_SCHEMA,
                },
            },
        }
        data, latency_ms, quota = await post_json(
            self._client,
            provider=self.provider,
            url=URL,
            headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
            body=body,
            timeout_seconds=request.timeout_seconds,
            secrets=(self._api_key,),
            # Strict mode still lets a bad generation through; Groq then answers 400 with this code.
            invalid_output_codes=frozenset({"json_validate_failed"}),
        )
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
            usage = data["usage"]
            input_tokens, output_tokens = (
                int(usage["prompt_tokens"]),
                int(usage["completion_tokens"]),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            raise InvalidOutput("groq: the response has an unexpected shape") from None
        if choice.get("finish_reason") == "length" or not isinstance(text, str):
            raise InvalidOutput("groq: the output was cut off or empty")
        details = usage.get("completion_tokens_details")
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        return AnswerResult(
            answer=parse_answer(text),
            usage=Usage(
                input_tokens, output_tokens, reasoning if isinstance(reasoning, int) else None
            ),
            latency_ms=latency_ms,
            provider=self.provider,
            model=self.model,
            quota=quota,
        )

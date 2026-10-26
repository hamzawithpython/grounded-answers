"""
The three adapters against fake HTTP (ADR 0031). No test calls a provider: CI has no keys and no network use here.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from grounded_answers.answering import (
    AnswerModel,
    InvalidOutput,
    ProviderRateLimited,
    ProviderRefused,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
)
from grounded_answers.answering.adapters import (
    AnthropicAnswerModel,
    GeminiAnswerModel,
    GroqAnswerModel,
)

from .answering_support import (
    REQUEST,
    Wire,
    anthropic_response,
    gemini_response,
    groq_response,
    replying,
)

pytestmark = pytest.mark.anyio

KEY = SecretStr("test-key-0123456789")

# (name, adapter factory, canned success response)
Factory = Callable[[httpx.AsyncClient], AnswerModel]
PROVIDERS: list[tuple[str, Factory, Callable[..., dict[str, Any]]]] = [
    ("groq", lambda c: GroqAnswerModel(KEY, c), groq_response),
    ("gemini", lambda c: GeminiAnswerModel(KEY, c), gemini_response),
    ("anthropic", lambda c: AnthropicAnswerModel(KEY, c), anthropic_response),
]
EACH = pytest.mark.parametrize(("name", "make", "ok"), PROVIDERS, ids=[p[0] for p in PROVIDERS])


@EACH
async def test_a_good_response_becomes_an_answer_with_usage(
    name: str, make: Factory, ok: Callable[..., dict[str, Any]]
) -> None:
    wire = replying(200, ok(), {"x-ratelimit-remaining-tokens": "7000", "set-cookie": "no"})
    result = await make(wire.client).answer(REQUEST)
    assert (result.answer.answered, result.answer.answer, result.answer.citations) == (
        True,
        "Thirty days.",
        (1,),
    )
    assert result.provider == name
    assert result.usage.input_tokens > 0 and result.usage.output_tokens > 0
    assert result.latency_ms >= 0
    # Only the rate-limit headers are kept.
    assert dict(result.quota) == {"x-ratelimit-remaining-tokens": "7000"}


@EACH
async def test_the_key_is_in_a_header_and_nowhere_else(
    name: str, make: Factory, ok: Callable[..., dict[str, Any]]
) -> None:
    wire = replying(200, ok())
    await make(wire.client).answer(REQUEST)
    sent = wire.requests[-1]
    assert KEY.get_secret_value() not in str(sent.url)
    assert KEY.get_secret_value() not in sent.content.decode()
    assert sent.url.query == b""
    assert any(KEY.get_secret_value() in v for v in sent.headers.values())


@EACH
async def test_the_schema_is_sent_and_the_request_is_deterministic(
    name: str, make: Factory, ok: Callable[..., dict[str, Any]]
) -> None:
    wire = replying(200, ok())
    await make(wire.client).answer(REQUEST)
    body = wire.last_body
    assert "Question: How many days of notice?" in json.dumps(body)
    assert "[1] Notice is thirty days." in json.dumps(body)
    assert "answered" in json.dumps(body)


async def test_groq_asks_for_strict_schema_and_low_reasoning() -> None:
    wire = replying(200, groq_response())
    await GroqAnswerModel(KEY, wire.client).answer(REQUEST)
    body = wire.last_body
    assert wire.requests[-1].url.host == "api.groq.com"
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["reasoning_effort"] == "low"
    assert body["include_reasoning"] is False
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert wire.requests[-1].headers["authorization"] == f"Bearer {KEY.get_secret_value()}"


async def test_groq_reports_reasoning_tokens_when_it_sends_them_and_none_when_not() -> None:
    wire = replying(200, groq_response(reasoning=40))
    result = await GroqAnswerModel(KEY, wire.client).answer(REQUEST)
    assert (
        result.usage.input_tokens,
        result.usage.output_tokens,
        result.usage.reasoning_tokens,
    ) == (
        1100,
        90,
        40,
    )
    wire = replying(200, groq_response(reasoning=None))
    assert (await GroqAnswerModel(KEY, wire.client).answer(REQUEST)).usage.reasoning_tokens is None


async def test_gemini_uses_the_header_and_a_schema_without_additional_properties() -> None:
    wire = replying(200, gemini_response())
    await GeminiAnswerModel(KEY, wire.client).answer(REQUEST)
    sent = wire.requests[-1]
    assert str(sent.url).endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert sent.headers["x-goog-api-key"] == KEY.get_secret_value()
    config = wire.last_body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert "additionalProperties" not in config["responseSchema"]
    assert config["responseSchema"]["required"] == ["answered", "answer", "citations"]


async def test_gemini_thinking_tokens_are_billed_as_output() -> None:
    wire = replying(200, gemini_response(thoughts=200))
    usage = (await GeminiAnswerModel(KEY, wire.client).answer(REQUEST)).usage
    assert (usage.output_tokens, usage.reasoning_tokens) == (60 + 200, 200)


async def test_anthropic_headers_and_schema() -> None:
    wire = replying(200, anthropic_response())
    await AnthropicAnswerModel(KEY, wire.client).answer(REQUEST)
    sent = wire.requests[-1]
    assert sent.headers["x-api-key"] == KEY.get_secret_value()
    assert sent.headers["anthropic-version"] == "2023-06-01"
    body = wire.last_body
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False


@EACH
async def test_429_is_rate_limited_with_the_providers_hint(
    name: str, make: Factory, ok: Callable[..., dict[str, Any]]
) -> None:
    wire = replying(429, {"error": {"message": "slow down"}}, {"retry-after": "12"})
    with pytest.raises(ProviderRateLimited) as caught:
        await make(wire.client).answer(REQUEST)
    assert caught.value.retry_after_seconds == 12.0


@EACH
async def test_429_without_a_usable_hint_has_none(
    name: str, make: Factory, ok: Callable[..., dict[str, Any]]
) -> None:
    wire = replying(429, {}, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
    with pytest.raises(ProviderRateLimited) as caught:
        await make(wire.client).answer(REQUEST)
    assert caught.value.retry_after_seconds is None


@EACH
async def test_a_server_error_is_unavailable(
    name: str, make: Factory, ok: Callable[..., dict[str, Any]]
) -> None:
    with pytest.raises(ProviderUnavailable):
        await make(replying(503, {}).client).answer(REQUEST)


@EACH
async def test_a_timeout_and_a_network_error_are_typed(
    name: str, make: Factory, ok: Callable[..., dict[str, Any]]
) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("took too long", request=request)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("https://example.test/?key=SECRET", request=request)

    with pytest.raises(ProviderTimeout):
        await make(Wire(slow).client).answer(REQUEST)
    with pytest.raises(ProviderUnavailable) as caught:
        await make(Wire(down).client).answer(REQUEST)
    # The library's text (which can hold a URL) is dropped.
    assert "SECRET" not in str(caught.value)


@EACH
async def test_a_rejected_request_keeps_a_short_provider_message_without_the_key(
    name: str, make: Factory, ok: Callable[..., dict[str, Any]]
) -> None:
    echoing = {"error": {"message": f"bad key {KEY.get_secret_value()} " + "x" * 500}}
    with pytest.raises(ProviderRejected) as caught:
        await make(replying(401, echoing).client).answer(REQUEST)
    text = str(caught.value)
    assert caught.value.status == 401
    assert KEY.get_secret_value() not in text
    assert "[redacted]" in text
    assert len(text) < 300


@EACH
async def test_output_that_is_not_the_schema_is_invalid(
    name: str, make: Factory, ok: Callable[..., dict[str, Any]]
) -> None:
    bad_outputs = [
        "not json at all",
        json.dumps({"answered": True, "answer": "x"}),
        json.dumps({"answered": True, "answer": "x", "citations": [1], "extra": 1}),
        json.dumps({"answered": "yes", "answer": "x", "citations": []}),
        json.dumps({"answered": True, "answer": "x", "citations": ["1"]}),
        json.dumps({"answered": True, "answer": "x", "citations": [True]}),
        json.dumps([1, 2]),
    ]
    for output in bad_outputs:
        with pytest.raises(InvalidOutput) as caught:
            await make(replying(200, ok(output)).client).answer(REQUEST)
        assert "at all" not in str(caught.value)


async def test_groq_json_validate_failed_is_invalid_output_not_a_bad_request() -> None:
    """Seen live (Step 3b): strict mode, and the model's JSON still broke the schema. Groq answers 400."""
    failed = {
        "error": {
            "message": "Generated JSON does not match the expected schema.",
            "type": "invalid_request_error",
            "code": "json_validate_failed",
            "failed_generation": '{"answered": true, "answer": "SECRET TEXT", "citations": ["1"]}',
        }
    }
    with pytest.raises(InvalidOutput) as caught:
        await GroqAnswerModel(KEY, replying(400, failed).client).answer(REQUEST)
    assert "SECRET TEXT" not in str(caught.value)
    # Any other 400 stays a rejected request.
    with pytest.raises(ProviderRejected):
        await GroqAnswerModel(
            KEY, replying(400, {"error": {"message": "bad", "code": "other"}}).client
        ).answer(REQUEST)


async def test_a_cut_off_output_is_invalid_not_parsed() -> None:
    truncated = json.dumps({"answered": True, "answer": "Thirty", "citations": [1]})
    with pytest.raises(InvalidOutput):
        await GroqAnswerModel(
            KEY, replying(200, groq_response(truncated, finish="length")).client
        ).answer(REQUEST)
    with pytest.raises(InvalidOutput):
        await GeminiAnswerModel(
            KEY, replying(200, gemini_response(truncated, finish="MAX_TOKENS")).client
        ).answer(REQUEST)
    with pytest.raises(InvalidOutput):
        await AnthropicAnswerModel(
            KEY, replying(200, anthropic_response(truncated, stop="max_tokens")).client
        ).answer(REQUEST)


async def test_refusals_are_typed_and_never_become_answers() -> None:
    with pytest.raises(ProviderRefused):
        await GeminiAnswerModel(KEY, replying(200, gemini_response(finish="SAFETY")).client).answer(
            REQUEST
        )
    blocked = {"promptFeedback": {"blockReason": "OTHER"}}
    with pytest.raises(ProviderRefused):
        await GeminiAnswerModel(KEY, replying(200, blocked).client).answer(REQUEST)
    with pytest.raises(ProviderRefused):
        await AnthropicAnswerModel(
            KEY, replying(200, anthropic_response("", stop="refusal")).client
        ).answer(REQUEST)


@EACH
async def test_an_unexpected_response_shape_is_invalid_output(
    name: str, make: Factory, ok: Callable[..., dict[str, Any]]
) -> None:
    with pytest.raises(InvalidOutput):
        await make(replying(200, {"surprise": True}).client).answer(REQUEST)

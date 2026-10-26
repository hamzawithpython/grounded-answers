"""
The one place an adapter talks HTTP, so key handling and error mapping are written once (ADR 0031, decision 7).

- The key travels in a header, never in the URL, and only `post_json` callers read it (`get_secret_value`).
- Errors name the provider and the status. A provider's own error text is kept only for 4xx answers, cut to 200
  characters, with the key replaced if the server echoed it. A network error's text is dropped (it can hold a URL).
- Prompts, answers and response bodies are never logged. The HTTP libraries' own loggers are held at WARNING, because
  httpcore logs request headers at DEBUG.
"""

import logging
import re
import time
from collections.abc import Mapping
from typing import Any, Final

import httpx
from pydantic import SecretStr

from grounded_answers.answering.types import (
    InvalidOutput,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)

_QUIET_LOGGERS: Final = ("httpx", "httpcore")
_MAX_PROVIDER_TEXT: Final = 200
_RETRY_AFTER: Final = re.compile(r"^\d+(\.\d+)?$")


def make_client() -> httpx.AsyncClient:
    """A client for adapters. Keeps the HTTP libraries' loggers at WARNING so headers cannot reach a log."""
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return httpx.AsyncClient()


def _scrub(text: str, secrets: tuple[SecretStr, ...]) -> str:
    for secret in secrets:
        value = secret.get_secret_value()
        if value:
            text = text.replace(value, "[redacted]")
    return text


def _provider_text(response: httpx.Response, secrets: tuple[SecretStr, ...]) -> str:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return ""
    if isinstance(error, dict):
        message = error.get("message")
    else:
        message = error
    return _scrub(message[:_MAX_PROVIDER_TEXT], secrets) if isinstance(message, str) else ""


def _error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    return float(raw) if _RETRY_AFTER.match(raw) else None


async def post_json(
    client: httpx.AsyncClient,
    *,
    provider: str,
    url: str,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    timeout_seconds: float,
    secrets: tuple[SecretStr, ...],
    invalid_output_codes: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], float, dict[str, str]]:
    """POST a JSON body. Returns (the JSON response, latency in ms, the x-ratelimit-* headers).

    `invalid_output_codes` are error codes that mean "the model's output broke the schema" although the provider
    answered with an HTTP error (Groq's `json_validate_failed`). They raise InvalidOutput, not ProviderRejected,
    so one bad generation does not look like a bad request. The failed generation itself is not kept."""
    started = time.perf_counter()
    try:
        response = await client.post(
            url, headers=dict(headers), json=body, timeout=httpx.Timeout(timeout_seconds)
        )
    except httpx.TimeoutException:
        raise ProviderTimeout(f"{provider}: no response within {timeout_seconds:g} s") from None
    except httpx.TransportError:
        raise ProviderUnavailable(f"{provider}: network error") from None
    latency_ms = (time.perf_counter() - started) * 1000
    status = response.status_code
    if status == 429:
        raise ProviderRateLimited(f"{provider}: rate limited (429)", _retry_after(response))
    if status >= 500:
        raise ProviderUnavailable(f"{provider}: server error ({status})")
    if status == 400 and _error_code(response) in invalid_output_codes:
        raise InvalidOutput(f"{provider}: the model's output did not match the schema")
    if status != 200:
        detail = _provider_text(response, secrets)
        raise ProviderRejected(
            f"{provider}: HTTP {status}" + (f": {detail}" if detail else ""), status
        )
    try:
        data = response.json()
    except ValueError:
        raise InvalidOutput(f"{provider}: the response is not JSON") from None
    if not isinstance(data, dict):
        raise InvalidOutput(f"{provider}: the response is not a JSON object")
    quota = {k: v for k, v in response.headers.items() if k.lower().startswith("x-ratelimit-")}
    return data, latency_ms, quota

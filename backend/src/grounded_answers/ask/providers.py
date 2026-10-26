"""
The answer-model adapters the web process can use (ADR 0031, ADR 0033 decision 5).

An adapter exists for a provider only when its key is configured. A tenant whose first provider has no adapter gets a
503 and a `service_error` row, never a call. The keys come from Settings (SecretStr, backend/.env only). One shared
httpx client serves all adapters; the lifespan creates and closes it.

`register` exists so tests can put a fake adapter in the place of a real one.
"""

import httpx

from grounded_answers.answering.adapters.anthropic import AnthropicAnswerModel
from grounded_answers.answering.adapters.gemini import GeminiAnswerModel
from grounded_answers.answering.adapters.groq import GroqAnswerModel
from grounded_answers.answering.types import AnswerModel
from grounded_answers.config import Settings


class ProviderRegistry:
    def __init__(self, adapters: dict[str, AnswerModel] | None = None) -> None:
        self._adapters: dict[str, AnswerModel] = dict(adapters or {})

    @classmethod
    def from_settings(cls, settings: Settings, client: httpx.AsyncClient) -> "ProviderRegistry":
        adapters: dict[str, AnswerModel] = {}
        if settings.groq_api_key is not None:
            adapters["groq"] = GroqAnswerModel(settings.groq_api_key, client)
        if settings.gemini_api_key is not None:
            adapters["gemini"] = GeminiAnswerModel(settings.gemini_api_key, client)
        if settings.anthropic_api_key is not None:
            adapters["anthropic"] = AnthropicAnswerModel(settings.anthropic_api_key, client)
        return cls(adapters)

    def get(self, name: str) -> AnswerModel | None:
        return self._adapters.get(name)

    def register(self, name: str, model: AnswerModel) -> None:
        self._adapters[name] = model

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

"""
The answer contract (ADR 0032, decision 1): what the model must return, and the parse of what came back.

Four fields and nothing else. A refusal has an empty answer and no citations, so the schema needs no null types, which
Gemini's schema subset and Anthropic's limits handle poorly. Every property is required and every object closes with
`additionalProperties: false`, as Groq's strict mode demands. No numeric or length constraints (Anthropic rejects them);
lengths are checked in validate.py instead.

`refusal_reason` is a fixed code. The model never writes text that a user sees on a refusal: the wording is ours
(messages.py).
"""

import json
from typing import Any, Final

from grounded_answers.answering.types import Citation, InvalidOutput, ModelAnswer

ANSWER_SCHEMA_NAME: Final = "grounded_answer"
NO_REFUSAL: Final = "none"
REFUSAL_REASONS: Final = ("not_in_documents", "needs_clarification", "conflicting_sources")

ANSWER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "refused": {"type": "boolean"},
        "refusal_reason": {"type": "string", "enum": [NO_REFUSAL, *REFUSAL_REASONS]},
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ref": {"type": "integer"}, "quote": {"type": "string"}},
                "required": ["ref", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["refused", "refusal_reason", "answer", "citations"],
    "additionalProperties": False,
}


def without_additional_properties(schema: Any) -> Any:
    """A copy with every `additionalProperties` removed, for Gemini, whose schema subset does not have it."""
    if isinstance(schema, dict):
        return {
            k: without_additional_properties(v)
            for k, v in schema.items()
            if k != "additionalProperties"
        }
    if isinstance(schema, list):
        return [without_additional_properties(v) for v in schema]
    return schema


def _citation(raw: Any) -> Citation:
    if not isinstance(raw, dict) or set(raw) != {"ref", "quote"}:
        raise InvalidOutput("a citation does not have exactly ref and quote")
    ref, quote = raw["ref"], raw["quote"]
    if not isinstance(ref, int) or isinstance(ref, bool) or not isinstance(quote, str):
        raise InvalidOutput("a citation has the wrong types")
    return Citation(ref, quote)


def parse_answer(text: str) -> ModelAnswer:
    """The model's JSON text as a ModelAnswer. Raises InvalidOutput, without echoing the text."""
    try:
        data = json.loads(text)
    except ValueError:
        raise InvalidOutput("the output is not JSON") from None
    if not isinstance(data, dict) or set(data) != set(ANSWER_SCHEMA["required"]):
        raise InvalidOutput("the output does not have exactly the schema's properties")
    refused, reason, answer, citations = (
        data["refused"],
        data["refusal_reason"],
        data["answer"],
        data["citations"],
    )
    if not isinstance(refused, bool) or not isinstance(answer, str):
        raise InvalidOutput("refused or answer has the wrong type")
    if reason not in (NO_REFUSAL, *REFUSAL_REASONS):
        raise InvalidOutput("refusal_reason is not one of the codes")
    if not isinstance(citations, list):
        raise InvalidOutput("citations is not a list")
    return ModelAnswer(refused, reason, answer, tuple(_citation(c) for c in citations))

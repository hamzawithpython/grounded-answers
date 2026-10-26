"""
The structured answer a model must return, and the check of what came back.

Provisional (ADR 0031): Step 4 fixes the real contract (citation validation, refusal wording). The shape stays
small so that all three providers accept it: every property required, no extra properties, no numeric or length
constraints (Anthropic rejects them), no recursion.
"""

import json
from typing import Any, Final

from grounded_answers.answering.types import InvalidOutput, ModelAnswer

ANSWER_SCHEMA_NAME: Final = "grounded_answer"
ANSWER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "answered": {"type": "boolean"},
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["answered", "answer", "citations"],
    "additionalProperties": False,
}


def parse_answer(text: str) -> ModelAnswer:
    """The model's JSON text as a ModelAnswer. Raises InvalidOutput, without echoing the text."""
    try:
        data = json.loads(text)
    except ValueError:
        raise InvalidOutput("the output is not JSON") from None
    if not isinstance(data, dict) or set(data) != set(ANSWER_SCHEMA["required"]):
        raise InvalidOutput("the output does not have exactly the schema's properties")
    answered, answer, citations = data["answered"], data["answer"], data["citations"]
    if not isinstance(answered, bool) or not isinstance(answer, str):
        raise InvalidOutput("answered or answer has the wrong type")
    if not isinstance(citations, list) or not all(
        isinstance(n, int) and not isinstance(n, bool) for n in citations
    ):
        raise InvalidOutput("citations is not a list of integers")
    return ModelAnswer(answered, answer, tuple(citations))

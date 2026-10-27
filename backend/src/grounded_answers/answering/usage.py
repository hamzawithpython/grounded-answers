"""
The coded vocabulary of the metering table (ADR 0033 decision 3, migration 0017).

`answer_usage` stores a request's outcome and the codes of the invalid outputs it saw, never text. The database
refuses any value outside these lists (CHECK constraints), so a bug cannot store the question or an answer there. The
lists are repeated in the migration, because a migration must not import application code; tests compare the two, and
check that every code validate.py can return, and every refusal kind, is on a list.
"""

from typing import Final

# What happened to a request that reached the answer stage. The first group is the result of answer_question.
OUTCOMES: Final = (
    "answered",
    "refused_not_in_documents",
    "refused_needs_clarification",
    "refused_conflicting_sources",
    "could_not_verify",
    "declined",
    "no_sources",
    # Failures: the user saw a 503 (ADR 0033 decision 2).
    "timeout",
    "provider_error",
    "provider_rate_limited",
    "provider_disallowed",
    "tenant_not_configured",
    "service_error",
)

# "invalid_output" is a reply that could not be parsed (adapter level); the rest come from validate.py.
INVALID_CODES: Final = (
    "invalid_output",
    "empty_answer",
    "no_citations",
    "bad_ref",
    "quote_ellipsis",
    "quote_too_short",
    "quote_too_long",
    "quote_not_informative",
    "quote_not_found",
    "number_not_grounded",
    "answer_language",
    "prompt_echo",
)

_REFUSAL_OUTCOMES: Final = {
    "not_in_documents": "refused_not_in_documents",
    "needs_clarification": "refused_needs_clarification",
    "conflicting_sources": "refused_conflicting_sources",
    "could_not_verify": "could_not_verify",
    "declined": "declined",
}


def outcome_for_refusal(kind: str) -> str:
    """The metering outcome for a Refusal.kind from answer_question."""
    return _REFUSAL_OUTCOMES[kind]

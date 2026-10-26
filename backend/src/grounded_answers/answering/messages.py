"""
What a user sees when there is no answer (ADR 0032, decision 4). Fixed English text written by us. A model never
writes it, so a refusal cannot carry text that came from a document or an injected instruction.
"""

from typing import Final

REFUSAL_MESSAGES: Final = {
    "not_in_documents": (
        "I couldn't find this in your company's documents. "
        "Please check with your manager or the department that owns the policy."
    ),
    "needs_clarification": "I couldn't tell what you're asking. Could you add more detail?",
    "conflicting_sources": (
        "The documents I found don't agree on this. Please check with the department that owns the policy."
    ),
    "could_not_verify": (
        "I couldn't produce an answer I can back with your documents. "
        "Please rephrase, or check with the department that owns the policy."
    ),
    "declined": (
        "I can't help with that request. Please rephrase it, or check with the department that owns the policy."
    ),
}

# Not a refusal: the model service failed. /ask answers 503 with this text (Step 5).
SERVICE_UNAVAILABLE_MESSAGE: Final = (
    "The answer service isn't available right now. Please try again in a few minutes."
)

"""
Which answer providers a tenant may use (ADR 0031 decision 3, ADR 0033 decision 5).

Two facts decide it: the tenant's data class and the provider's terms. The free tiers of Groq and Gemini may be used
or kept in ways we cannot rule out (ADR 0031), so they take fictional data only. A provider is approved for real data
only when its terms, for the way we call it, say that submitted data is not used for training and a person has checked
that. Today that is Anthropic with the client's own paid key. Adding a provider to APPROVED_FOR_REAL_DATA is a reviewed
change with an ADR.

The same function runs when the operator sets a tenant's providers (admin CLI) and, in Step 5c, before every call to a
provider. The list of known providers is repeated in a CHECK of migration 0017; a test compares the two.
"""

from collections.abc import Sequence
from typing import Final

KNOWN_PROVIDERS: Final = ("groq", "gemini", "anthropic")
APPROVED_FOR_REAL_DATA: Final = frozenset({"anthropic"})
DATA_CLASSES: Final = ("fictional", "real")
MAX_PROVIDERS: Final = len(KNOWN_PROVIDERS)


class ProviderPolicyError(ValueError):
    """A provider list or data class the policy refuses. The message names the rule, never a secret."""


def check_data_class(data_class: str) -> str:
    if data_class not in DATA_CLASSES:
        raise ProviderPolicyError(f"Data class must be one of: {', '.join(DATA_CLASSES)}.")
    return data_class


def check_providers(providers: Sequence[str], data_class: str) -> tuple[str, ...]:
    """Return the providers as a tuple, in order, or raise ProviderPolicyError."""
    check_data_class(data_class)
    chosen = tuple(providers)
    if len(set(chosen)) != len(chosen):
        raise ProviderPolicyError("A provider is listed twice.")
    unknown = [name for name in chosen if name not in KNOWN_PROVIDERS]
    if unknown:
        raise ProviderPolicyError(f"Unknown provider. Known: {', '.join(KNOWN_PROVIDERS)}.")
    if data_class == "real":
        refused = [name for name in chosen if name not in APPROVED_FOR_REAL_DATA]
        if refused:
            raise ProviderPolicyError(
                f"Not approved for real data: {', '.join(refused)}. "
                f"Approved: {', '.join(sorted(APPROVED_FOR_REAL_DATA))}."
            )
    return chosen


def is_allowed(provider: str, providers: Sequence[str], data_class: str) -> bool:
    """True when this provider is on the tenant's list and the policy allows it for the tenant's data class."""
    try:
        return provider in check_providers(providers, data_class)
    except ProviderPolicyError:
        return False

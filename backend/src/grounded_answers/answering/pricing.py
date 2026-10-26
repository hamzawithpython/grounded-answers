"""
List prices, in US dollars per million tokens (ADR 0031). Read from the providers' pages on 2026-10-26; check them
again before relying on a figure. A free tier costs nothing: this is what the same tokens would cost on the paid tier,
so the SPEC target (at most $0.002 per answer) can be checked while testing on free tiers.
"""

from typing import Final

from grounded_answers.answering.types import Usage

# model -> (input, output) per million tokens.
LIST_PRICES: Final[dict[str, tuple[float, float]]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "claude-haiku-5-5": (0.10, 0.50),
}


class UnknownModelPrice(KeyError):
    """No list price for this model. Add it to LIST_PRICES from the provider's page."""


def list_price_usd(model: str, usage: Usage) -> float:
    try:
        input_price, output_price = LIST_PRICES[model]
    except KeyError:
        raise UnknownModelPrice(model) from None
    return (usage.input_tokens * input_price + usage.output_tokens * output_price) / 1_000_000

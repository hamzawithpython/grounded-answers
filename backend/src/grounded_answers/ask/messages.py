"""
What /ask says when it does not answer (ADR 0033 decisions 2 and 6). Fixed English text written by us, keyed by the
public code; the body carries the code and this text and nothing about which stage or provider failed.
"""

from typing import Final

LIMIT_MESSAGES: Final = {
    "rate_limited": "You are asking questions too quickly. Please wait a moment and try again.",
    "busy": "Several questions from your company are being answered right now. Please try again in a moment.",
    "daily_limit": "Your company has reached its daily question limit. It resets at 00:00 UTC.",
}

UNAVAILABLE_MESSAGES: Final = {
    "not_configured": "The assistant is not set up for your company yet. Please contact your administrator.",
    "try_again": "The assistant could not answer in time. Please try again.",
    "service_unavailable": "The assistant is not available right now. Please try again in a few minutes.",
}

INTERNAL_ERROR_MESSAGE: Final = "Something went wrong. Please try again."

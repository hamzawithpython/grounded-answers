"""
The failure vocabulary of ingestion (ADR 0020, amended by ADR 0022 decision D4).

Every failure stored on a version is one of these generic reasons. A child process's stderr, a traceback or
document text is never stored or logged (ADR 0020).
"""

from typing import Final

from grounded_answers.extract.sandbox import (
    REASON_MEMORY,
    REASON_NO_TEXT,
    REASON_TIMEOUT,
    REASON_UNREADABLE,
)

# The embedding model failed on this document's text (ADR 0022, decision D4).
REASON_EMBEDDING: Final = "embedding"

# Every attempt hit a transient error, so the job gave up (ADR 0022, decision D5). This is not an extraction
# reason: an admin must be able to tell a bad document from an outage.
REASON_RETRIES_EXHAUSTED: Final = "retries_exhausted"

FAILURE_REASONS: Final = frozenset(
    {
        REASON_TIMEOUT,
        REASON_MEMORY,
        REASON_UNREADABLE,
        REASON_NO_TEXT,
        REASON_EMBEDDING,
        REASON_RETRIES_EXHAUSTED,
    }
)


class VersionNotFound(Exception):
    """No version with this id is visible to the job's tenant. Permanent: the job is never retried (ADR 0015)."""


class LeaseHeld(Exception):
    """Another attempt holds a fresh lease on the version. Transient: the job is retried later."""


class LostLease(Exception):
    """This attempt no longer holds the version (its token no longer matches). It writes nothing (ADR 0022, D1)."""

"""
Correlation values for log lines (ADR 0033, decision 8: the project-wide rule).

Raw tenant, user and document ids never appear in log lines (ADR 0017 kept them out of job logs; ADR 0033 extends
the rule to every request path, and a test scans the source for violations). Where a line must let an operator
follow the requests of one tenant, it carries a keyed hash instead: HMAC-SHA256 over "log:<kind>:<id>" with the
application's log key (AUTH_LOG_HMAC_KEY), as hex, first 16 characters.

The "log:" prefix and the kind keep the value different from the other uses of the same key. Without the key the
value cannot be turned back into an id or checked against a guessed one; rotating the key breaks correlation across
the rotation and reveals nothing. Only the tenant kind exists; add another when something needs it.
"""

import hashlib
import hmac
import uuid
from typing import Final

REF_CHARS: Final = 16


def tenant_log_ref(key: bytes, tenant_id: uuid.UUID) -> str:
    """The log correlation value for a tenant: 16 hex characters."""
    message = f"log:tenant:{tenant_id}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:REF_CHARS]

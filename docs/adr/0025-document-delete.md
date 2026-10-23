# ADR 0025: Deleting a document

**Status:** Accepted (Session 4, Step 7, PR 1). Built in PR 4.

**Date:** 2026-10-24

**Deciders:** Hamza Asif

## Context

ARCHITECTURE §5.3 says a delete is soft: the document is hidden at once, and purge removes the data after the retention period (ADR 0007). Retrieval must exclude the document as soon as the delete commits (§5.6). Purge only finds versions that are `retired` or `purging` (migration 0003), so every version must be retired by the delete. Otherwise purge would never reach it.

## Decision

`DELETE /documents/{id}` is tenant_admin only. It runs in **one app transaction**:

1. Set `documents.deleted_at = now()` and `purge_after = now() + DOCUMENT_RETENTION_DAYS`. The column check requires both or neither.
2. Retire every version that is not already `purging` or `retired`: `queued`, `processing`, `ready` and `failed` become `retired`, with `retired_at` and `purge_after`.
3. Write audit `document.delete` with `actor_user_id` and counts only (for example `{"versions": 3}`). No title and no filename.

A deleted or unknown document returns **404**. A second delete does not succeed.

**In-flight work:** a worker that is processing a version the delete retired writes `ready` with its fencing token. Its policy needs `status = 'processing'`, so it matches zero rows. The rowcount check (ADR 0022) treats that as a lost write and rolls back.

**The transition guard (ADR 0026)** lets app_user make exactly these retire steps, and nothing else.

## Alternatives

| Option | Pros | Cons | Cost |
|---|---|---|---|
| **A. One transaction that retires every version (chosen)** | Matches §5.3. Exclusion is immediate. Atomic. | Needs the per-role guard for app_user. | Low–medium |
| B. Set `deleted_at` only | Smallest change. | Versions stay unretired, so purge never finds them. | Low, wrong |
| C. Hard delete now | Simplest. | Breaks the retention promise. No undo. | Low, not recommended |

## Consequences

- **Positive:** a delete cannot leave a half-deleted document, and cannot be undone by a late worker write.
- **Negative:** `app_user` gains the power to retire versions. The guard limits it to retirement, and no other transition.

## Tests (PR 4)

- Immediate exclusion: the retrieval query returns nothing for the document after commit.
- The audit row has counts and no title or filename (ARCHITECTURE §5.7 #4).
- A late worker write after a delete matches zero rows and leaves the version retired.
- A second delete returns 404.
- The guard refuses app_user's other transitions (for example `ready → queued`).

## Related

- ADR 0007 (retention), ADR 0022 (rowcount), ADR 0026 (guard), ARCHITECTURE §5.3, §5.6.

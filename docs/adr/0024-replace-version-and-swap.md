# ADR 0024: Replacing a document version and the swap to current

**Status:** Accepted (Session 4, Step 7, PR 1). Built in PR 6.

**Date:** 2026-10-24

**Deciders:** Hamza Asif

## Context

A tenant admin uploads a new version of an existing document. The new version goes through the upload pipeline (ADR 0019) and the ingest job (ADR 0022). When it is ready, it must become the document's current version, and the old current version must be retired. Two replacements can finish in either order.

Today the worker sets `documents.current_version_id` only when it is NULL (`ingest/pipeline.py`, `worker_documents_set_current`, migration 0009). That is correct for the first version and not for replacements.

## Decision

1. **Endpoint:** `POST /documents/{id}/versions`, tenant_admin only. It reuses the upload pipeline and its limits (ADR 0019). `version_no` is the next number for the document. The unique key `(document_id, version_no)` makes two concurrent replacements fail with a conflict for one of them (HTTP 409).
2. **The swap is one security-definer function, `grounded.swap_current_version(version_id)`.** The worker calls it in the same transaction as its `ready` write. The function runs as the owner role `swap_definer` (`NOLOGIN`). It:
   - locks the document row (`SELECT ... FOR UPDATE`), so swaps on one document run one at a time;
   - refuses a version that is not `ready`, is not in the session tenant, or whose document is deleted;
   - **ordering:** if the document has no current version, or the finished version's `version_no` is higher than the current one's, it sets `current_version_id` to the finished version and retires the old current version (`retired_at`, `purge_after = now() + DOCUMENT_RETENTION_DAYS`);
   - otherwise (the finished version is older), it retires the finished version at once, as superseded, and leaves the current version as it is;
   - writes one audit row with counts only.
3. **Transition guard (ADR 0026):** `swap_definer` is listed in the trigger, with only the swap's transitions: `ready → retired`. It cannot set a version to `ready`.
4. **Policies:** `swap_definer` gets UPDATE on the two columns it changes, limited to the session tenant. Nothing else.

## Alternatives

| Option | Pros | Cons | Cost |
|---|---|---|---|
| **A. Definer function called by the worker (chosen)** | One atomic place, in the same transaction as `ready`. Worker policies stay narrow. | New definer surface, covered by ADR 0026. | Medium |
| B. Widen the worker's policies so it moves the pointer and retires ready versions itself | No new function. | The worker can retire any ready version in its tenant. The guard must allow it, which weakens it. | Medium, weaker |
| C. app_user swaps when it sees `ready` | No worker change. | Not atomic with `ready`. Two replacements can race. | Low, risky |

## Consequences

- **Positive:** a version is current only if it is newer than the current one, so an older finisher cannot roll the document back. The swap is atomic with `ready`.
- **Negative:** one more definer function, reviewed under SECURITY.md.

## Tests (PR 6)

- **Race (a):** two replacements (v2, v3) finish together. One is current, and the old version is retired exactly once.
- **Race (b):** v3 finishes before v2. v3 stays current, and v2 is retired at once.
- A deleted document is refused. A version from another tenant is refused.
- The function's owner role can make only the swap's transitions (ADR 0026 test matrix).

## Related

- ADR 0019 (upload), ADR 0022 (the write and the fence), ADR 0025 (delete), ADR 0026 (guard).

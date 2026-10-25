# ADR 0024: Replacing a document version and the swap to current

**Status:** Accepted (Session 4, Step 7, PR 1). Built in PR 6; amended in PR 6 (row lock chosen over an advisory lock, lock order, swap signature, worker narrowing).

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

## Amendment (PR 6, 2026-10-25): the lock, the lock order, and what the build settled

- **Serialization point: the document row, locked with `SELECT ... FOR UPDATE`.** Replacement, the swap, and delete all take it. An advisory lock was rejected: delete would have had to take it as well, and keys can collide. The row lock adds no privilege that the function does not already have: the locking clause needs UPDATE on some column, which swap_definer holds on `current_version_id`, and app_user holds on `title`.
- **Lock order: document, then versions, then chunks.** The finish used to write the version first. It now locks the live document first, so a finish and a delete of the same document cannot deadlock (`test_a_delete_and_a_finish_on_the_same_document_do_not_deadlock`).
- **worker_user's lock-only policy.** PostgreSQL's CREATE POLICY reference (the table of policy commands) says that `SELECT FOR UPDATE` is filtered by the USING of SELECT and UPDATE policies, and that no WITH CHECK applies to a lock. So `worker_documents_lock` (FOR UPDATE, USING the tenant and a live document, WITH CHECK false) lets the worker lock a live document and refuses every UPDATE of it. Tested in `tests/db/test_worker_document_lock.py`.
- **A lock on a deleted document succeeds, observed.** The purge policy (migration 0003) admits deleted rows, and permissive policies are OR-ed. So the finish does not rely on the lock to detect a delete: its live-document check and the version's status and fencing token decide.
- **The swap's signature is (document_id, version_id).** The function checks that the version belongs to the document and to the session tenant.
- **The swap decides for every version.** The finish no longer sets the pointer itself. The worker's set-current policy (0009) is removed. The worker keeps UPDATE on `current_version_id` for the purge clear, since the lock needs an UPDATE privilege.
- **The retention period is temporary** (30 days, in the function) until PR 7.

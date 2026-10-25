# ADR 0029: Listing documents for tenant admins

**Status:** Accepted (Session 4, Step 7, PR 1). Built in PR 5 (`GET /documents`, `documents_list.py`).

**Date:** 2026-10-24

**Deciders:** Hamza Asif

## Context

Step 8 needs the admin to see ingestion results: which documents are queued, processing, ready, or failed, and why. No endpoint lists documents today.

## Decision

1. `GET /documents` is tenant_admin only. Tenant isolation comes from RLS (ADR 0003), so the query needs no tenant filter of its own.
2. **Cursor pagination,** ordered by `(created_at DESC, id DESC)`. Default page size 20, maximum 50.
3. **Fields per document:** `id`, `title`, `current_version_no` (or null), `latest_version_no`, `latest_status`, `latest_failure_reason` (generic reason only, ADR 0020), `updated_at`. The status is the **latest** version's (highest `version_no`), so a replacement that is still queued is visible. The current version number is shown separately.
4. Deleted documents are excluded (`deleted_at IS NULL`), as in §5.6.
5. Listing writes no audit row. Whether reads should be audited is a separate question, open for a later ADR.

## Alternatives

| Option | Pros | Cons | Cost |
|---|---|---|---|
| **A. Admin only, cursor, latest status (chosen)** | Shows what Step 8 needs. Cheap. | Titles are shown to admins, who can already see them. | Low |
| B. Also list every version | Full history. | Bigger response. Step 8 does not need it. | Low–medium |
| C. Defer to Step 8 | Less now. | Step 8 cannot show ingestion results. | None |

## Consequences

- **Positive:** the admin sees progress and failures without database access.
- **Negative:** the status shown is the latest version's, not the current one's. The response shows both.

## Tests (PR 5)

- Pagination: the cursor returns each document once, in order, across pages.
- A deleted document is absent.
- Another tenant's documents are absent (RLS).
- A non-admin member gets 403.
- The response has no document text, storage keys or hashes.

## Related

- ADR 0019 (upload), ADR 0020 (failure reasons), ADR 0022 (statuses), ARCHITECTURE §5.6.

## Amendment (PR 5, 2026-10-25): what the build settled

- **Two statuses per item.** `current_version_status` is null until a version is current. `latest_version_*` (id, number, status, failure reason) is the highest `version_no`, so a queued first version and a failed replacement are both visible. The current and latest versions can differ, and both are shown.
- **Cursor.** The cursor is an opaque, URL-safe encoding of the `(created_at, id)` of the last item. Pages are "strictly after this key", newest first. A document added between two requests sorts above the cursor, so it is never repeated and never displaces an item on a later page (`test_a_document_added_between_pages_repeats_and_skips_nothing`). The cursor is a position, not an authorization: RLS still decides what the caller sees.
- **Bad limit or cursor is 400, not 422.** FastAPI's default 422 has a list-shaped body. This API answers 400 with `{"detail": "..."}`, the same shape as its other errors. The messages are generic and do not echo the input.
- **`failure_reason` only leaves the server as a generic reason.** The column is free text. Only values in `FAILURE_REASONS` (ADR 0020, ADR 0022 D4 and D5) are returned; anything else is null, and is not echoed. A planted non-generic value is tested.
- **The walk is bounded in the tests.** A cursor that stops moving forward fails the test after 100 pages instead of hanging the suite. This was found when a planted cursor bug made the walk loop.

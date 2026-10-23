# ADR 0026: Per-role transition guard, and narrowing app_user's grants

**Status:** Accepted (Session 4, Step 7, PR 1). Built in PR 2 (migration `0010`), tested as a full matrix. swap_definer is added with its function in PR 6.

**Date:** 2026-10-24

**Deciders:** Hamza Asif

## Context

ADR 0022 D8 added a trigger, `document_versions_worker_transitions` (migration 0009), that restricts the status changes of `worker_user`. It does not restrict `app_user`. Today `app_user` holds `UPDATE (status, failure_reason, retired_at, purge_after)` on `document_versions` and `UPDATE (current_version_id)` on `documents` (migration 0002). The RLS policies only check the tenant. So an application bug can move a `queued` version to `ready`, or point a document at any version of its tenant.

Checked in PR 1: `documents.current_version_id` is written only by the worker (`ingest/pipeline.py`, when NULL). No app code path writes it. (The ORM only declares it.)

**Trigger keying:** the trigger tests `current_user`. A security-definer function runs with its owner's name as `current_user`, so each owner must be listed in the trigger, or it is not guarded.

## Decision

1. **Extend the existing trigger function** with a table of allowed transitions per role:

| Role | Allowed `status` changes |
|---|---|
| `worker_user` | queued → processing; processing → ready; processing → failed; retired → purging (unchanged) |
| `app_user` | queued → retired; processing → retired; ready → retired; failed → retired (delete, ADR 0025) |
| `swap_definer` (ADR 0024) | ready → retired (the swap only) |
| `retry_definer` (ADR 0023) | none; it changes no version status |

Any other change by a guarded role raises `insufficient_privilege` with the message `<role> may not move a version from X to Y`. The `migration_role` is not guarded, because it applies migrations and is used only from the shell (ADR 0004). This is stated, not assumed; the catalog test lists it.

2. **Revoke `UPDATE (current_version_id)` on `documents` from `app_user`.** Checked: no app code path uses it. Only the worker sets it, and the swap function does that for replacements.

3. The trigger function keeps the existing hardening: pinned `search_path`, EXECUTE revoked from PUBLIC.

## Alternatives

| Option | Pros | Cons | Cost |
|---|---|---|---|
| **A. One function, per-role table (chosen)** | One place. One test per pair. Same pattern as 0009. | Owners must be listed. | Medium |
| B. A trigger per role | Clear per role. | Trigger ordering risk. Duplicated code. | Medium |
| C. No direct UPDATE grants for app_user at all | Strongest. | Every status change needs a definer function. | High |

## Consequences

- **Positive:** app_user can retire versions and nothing else. A document's current pointer is set only by the worker and the swap.
- **Negative:** any new definer function that changes status must be added to the table, in the same change.

## Tests (PR 2)

- The transition matrix: every allowed and every forbidden pair, for each guarded role (including the definers).
- app_user cannot set `current_version_id` (`insufficient_privilege` from the grant).
- The catalog test lists the trigger's roles, so a new one fails until it is reviewed.

## Related

- ADR 0022 D8 (the original guard), ADR 0023, 0024, 0025.

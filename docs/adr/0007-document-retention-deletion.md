# ADR 0007: Document Retention and Deletion Lifecycle

**Status:** Accepted

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

SPEC §6.1 requires document upload, replacement and status tracking, but says nothing about deletion or how long deleted content is kept. Three requirements pull against each other:

- **Answers must stop citing a document the moment it is deleted.** An employee must never receive an answer based on a removed policy.
- **Data minimisation.** Employee documents should not be kept indefinitely after they are removed.
- **An audit trail.** Admins need proof that deletion and purge happened, without the audit log itself holding the content.

The data model (ARCHITECTURE.md §5) already has `retired` and `purging` version states, `deleted_at`, `purge_after` and a `purge` path. This ADR records the product decision behind them and the role design for the purge job.

## Options considered

| | A. Hard delete | B. Soft delete, kept forever | **C. Soft delete now, purge after retention** |
|---|---|---|---|
| **Answers stop citing it** | Immediately. | Immediately, through the retrieval filter. | Immediately, through the retrieval filter. |
| **Content after removal** | Gone immediately. | Kept indefinitely. | Kept for N days, then purged. |
| **Data minimisation** | Strong. | Weak. Employee files accumulate. | Strong after N days. |
| **Audit trail** | Action logged, content gone. | Full history kept. | Action logged; content gone after N days. |
| **Accidental deletion** | No recovery. | Recoverable, but no restore path in v1. | Recoverable during N days, if we add restore later. |
| **Effort** | Low. | Low. | Medium: one purge job, one worker role, tests for purge order. |

**Decision: C**, with N = 30 days by default.

## Decision

### Deletion and replacement

- **Delete:** one transaction sets `documents.deleted_at` and `purge_after`, retires every version, and writes audit entry `document.delete` with no title or filename. Retrieval excludes the document from the moment the transaction commits.
- **Replace:** on successful ingestion of a new version, one transaction makes the new version `ready`, points `documents.current_version_id` at it, and retires the old version. The old version has the same `purge_after` schedule. Answers keep citing the old version until the swap commits.
- **No restore in v1.** Restore is a later ADR if the business needs it.
- **Retention is recorded per row.** `purge_after` is written when a row is retired or deleted. Changing `DOCUMENT_RETENTION_DAYS` affects only rows retired afterwards.
- **Setting:** `DOCUMENT_RETENTION_DAYS`, integer, default 30, range 1–365, validated at startup.

### Purge job and the `worker_user` role

The purge job runs in a worker process, not the web app. It needs to read across tenants to find due work, and it must not get a general cross-tenant privilege.

**Design:**

1. **`worker_user`**: LOGIN, `NOBYPASSRLS`, not a table owner, no superuser. It is the worker's only database login.
2. **`retention_due_tenant_ids()`**: a narrow `SECURITY DEFINER` function. It returns **only tenant ids** that have a purge due. It returns no document data. It is owned by `retention_definer`, a `NOLOGIN`, `NOBYPASSRLS` role with a read-only policy on `documents` and `document_versions`, the only tables the function reads. It is hardened like the auth functions (ADR 0004): `SET search_path`, schema-qualified names, `REVOKE` from `PUBLIC`, `EXECUTE` granted to `worker_user` only.
3. **Per-tenant processing under normal RLS.** For each returned tenant id, the worker opens a transaction, runs `set_config('app.current_tenant', :tid, true)` (ADR 0005), and then processes that tenant's rows as `worker_user`. Every row it touches is subject to the same tenant policies as the web app. The worker can only delete rows that are already in `purging` state (see the policy list below).

**Why this shape:** the only cross-tenant read is one function that returns tenant ids. The worker never sees another tenant's data without the tenant context set, so a bug in the worker can't read across tenants in one query.

### Purge steps (children first, per tenant, per document)

1. Mark the due versions `purging` (a transaction under RLS). Retrieval excludes `purging` rows at once.
2. Delete dependent data: chunks and embeddings, from Session 3/4.
3. Delete the stored bytes at `storage_key`. The operation is idempotent: a missing object counts as deleted. This needs storage credentials that exist **only in the worker**, never in the web app.
4. Delete the version row. For a deleted document, first set `current_version_id = NULL`.
5. Delete the document row once no versions remain.
6. Write `document.purged` or `document_version.purged`, with `actor_user_id = NULL` and only counts in `detail`. No title, filename, hash or content.

A crash leaves rows in `purging`. The job is idempotent and resumes them.

### Database invariants (CHECK constraints)

These are enforced by the database, not left to the app:

- `documents`: `(deleted_at IS NULL) = (purge_after IS NULL)`.
- `document_versions`: `(retired_at IS NULL) = (purge_after IS NULL)`, and `status IN ('retired', 'purging') = (retired_at IS NOT NULL)`.

Cross-table invariants, such as "a deleted document has only retired versions", are enforced by the delete and swap transactions and covered by tests (ARCHITECTURE.md §5.7).

### `worker_user` privileges (summary; full grants in the bootstrap plan)

| Table | Privilege | Restriction |
|---|---|---|
| `documents` | SELECT, UPDATE (`current_version_id`), DELETE | DELETE only when `deleted_at IS NOT NULL` and no version rows remain. |
| `document_versions` | SELECT, UPDATE (`status`), DELETE | UPDATE only `retired` to `purging`. DELETE only when `status = 'purging'`. |
| `audit_log` | INSERT | Append-only. |
| `tenants`, `users`, `tenant_memberships`, token tables | none | The worker has no reason to touch them. |

These restrictions are RLS policies `TO worker_user`, so they hold even if the code is wrong.

## Consequences

- **Positive:** employee files are removed within a known window. Answers stop citing removed content at once. The audit trail records the action, not the content.
- **Positive:** the worker's cross-tenant reach is one function that returns ids, with RLS on everything else.
- **Negative: a third read-only `SECURITY DEFINER` function.** ADR 0004 said "two RLS exceptions". This ADR adds `retention_due_tenant_ids()` with its own `retention_definer` role, so the count is now three. ADR 0004 needs a one-line note.
- **Negative: backups.** Purge removes live data, not copies in database backups. Deleted content stays in backups until the backup retention window expires, which is set by the hosting provider. This must be stated in SECURITY.md and RUNBOOK, and the 30-day figure applies to live data only. Stated as a limit, not hidden.
- **Negative: no legal hold.** A document under legal hold can't be kept past its purge date in v1. If that's needed, it's a new ADR.
- **Negative: storage credentials in the worker.** The worker can delete bytes. That's the purge job's job, but a compromised worker can delete stored files. Mitigation: least-privilege storage credentials, prefix-scoped to `tenant/`.

## Alternatives considered

- **A. Hard delete:** simplest, but removes the audit's ability to show what was removed and when. Rejected for the reason above.
- **B. Soft delete forever:** keeps everything, which is weak minimisation for employee data. Rejected.
- **Purge in the web app:** rejected. Purge needs storage credentials and cross-tenant discovery, and neither belongs in a request path.

## Open questions

1. **Confirm N = 30 days** as the default.
2. **Backup window:** the hosting provider's backup retention needs to be recorded when the host is chosen. Please confirm that stating it in SECURITY.md is acceptable.

## Related decisions

- ADR 0004 (roles, RLS exceptions, a note about the third one)
- ADR 0005 (`set_config`, RLS)
- ADR 0006 (test database and bootstrap)
- ARCHITECTURE.md §5 (lifecycle), §7 (RLS summary)

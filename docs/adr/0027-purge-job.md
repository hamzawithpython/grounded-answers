# ADR 0027: Purge job for retired versions and deleted documents

**Status:** Accepted (Session 4, Step 7, PR 1). Built in PR 7; amended in PR 7 (the sweep role, the item rules, the setting).

**Date:** 2026-10-24

**Deciders:** Hamza Asif

## Context

ARCHITECTURE §5.5 describes the purge as a loop over tenants, with the steps children first. ADR 0007 and migration 0003 provide the policies and the tenant lookup `retention_due_tenant_ids()`. The purge is not built, and `DOCUMENT_RETENTION_DAYS` is not in `Settings` yet (checked in PR 1: no match in `backend/`).

## Decision

1. **Sweep.** A periodic task finds the tenants with due work, using the existing `retention_due_tenant_ids()`. For each tenant, it reads the due items **inside the tenant transaction** (normal RLS, so no new definer function). It defers one purge job per item, with `queueing_lock = "purge:<kind>:<id>"`. A duplicate defer raises `AlreadyEnqueued`, which the sweep ignores.
2. **One job per item, one transaction per job.** A job runs in `begin_worker_transaction` (ADR 0017) for its tenant. The steps, children first:
   - mark the versions `purging` (retired → purging, allowed by ADR 0026);
   - delete the chunks;
   - delete the bytes row in `document_blobs` (idempotent: a missing row is done);
   - for a deleted document, set `current_version_id = NULL`, then delete the versions, then the document;
   - write audit `document.purged` or `document_version.purged` with counts only.
   The whole item commits or rolls back together.
3. **Crash safety.** Because each item is one transaction, a crash rolls back the whole item, and the next sweep defers it again. The `purging` state is still set first, so it stays visible if bytes ever move to external storage (ADR 0018, the scale-up path). The §5.7 #3 test becomes: a job that fails after the byte deletion rolls back, and a re-run completes.
4. **Settings.** `DOCUMENT_RETENTION_DAYS`: integer, default 30, range 1 to 365, validated in `Settings` at startup. Added to `backend/.env.example` (which the test `test_env_example.py` checks against `Settings`).
5. **Retention is per row.** `purge_after` is written when a row is retired or deleted (§5.5). A changed setting affects only later rows.

## Alternatives

| Option | Pros | Cons | Cost |
|---|---|---|---|
| **A. Per-item jobs through the queue (chosen)** | Small transactions. A failure loses one item. Uses the queue's retries and locks. | More jobs. | Medium |
| B. One transaction per tenant (§5.5 as written) | Simple. | Long locks on big tenants. One stuck tenant blocks the rest. | Low |
| C. One purge task for everything | Simplest. | Breaks per-tenant isolation. | Low, not recommended |

## Consequences

- **Positive:** a half-purged item never commits, so retrieval never sees a partial state. A crash costs one item's work.
- **Negative:** one job per item means more queue rows (cleaned by ADR 0028).

## Tests (PR 7)

- Purge completeness (§5.7 #2): no versions, documents, bytes or chunks remain.
- Crash and resume: a failure after the byte deletion rolls back, and a re-run completes (§5.7 #3).
- Content-free audit (§5.7 #4).
- Two sweeps produce one job per item (queueing lock).
- Tenant scoping: tenant A's job never touches tenant B's rows (§5.7 #6).
- `DOCUMENT_RETENTION_DAYS` is refused outside 1 to 365, and `.env.example` matches `Settings`.

## Related

- ADR 0007 (retention), ADR 0017 (worker transaction), ADR 0018 (bytes), ADR 0026 (guard).

## Amendment (PR 7, 2026-10-25): what the build settled

- **The setting replaces both temporary constants.** `DOCUMENT_RETENTION_DAYS` (1 to 365, default 30) is in `Settings` (web app) and in `WorkerSettings` (worker), read from the same variable. The delete endpoint and the swap both stamp `purge_after` with it, and the worker passes it to the swap as an argument. The swap's signature changed to `(document_id, version_id, retention_days)` (migration `0014_purge`), so a retirement can never use a stale constant.
- **A new role, `purge_definer`, owns the sweep** (`grounded.enqueue_purge_jobs()`), instead of widening `retention_definer`. Its privileges are the queue grants of the enqueueing role, and two read policies that return ids and timestamps. worker_user may execute the function and nothing else.
- **Items.** A deleted document past its date (kind `document`) and a retired version of a live document past its date (kind `version`). A retired version of a deleted document is covered by its document's job, so it gets no job of its own. The sweep reads each item's due-ness at the time it runs, and a job checks it again under the document lock.
- **Lock order.** Every purge job locks the document row before it reads or writes any version. This is the order of replacement, the swap and delete (ADR 0024).
- **One transaction per item.** A failure rolls the whole item back. The rows stay retired, and the next sweep defers the item again. The `purging` state is set inside the transaction, so it is never committed on its own.
- **A document whose versions are not all removable is refused.** The job raises rather than deleting the document: a deleted document is removed only when none of its versions is left.
- **Content-free audit.** `document.purged` holds `versions`, `chunks` and `blobs` counts. `document_version.purged` holds `chunks` and `blobs`. Neither holds a title, a filename, or content.
- **Restores.** After a backup restore, the purge runs again before the app serves users (RUNBOOK, "Restore after a backup"). A due-items query returns the check.
- **Cadence.** The sweep runs every fifteen minutes. A due item waits at most that long after its date.

## Amendment (PR 9, 2026-10-25): the purge sweep runs on the worker's timer

The purge sweep is no longer a Procrastinate periodic task (the periodic deferrer needs rights worker_user must not have; see ADR 0023's amendment). The sweep, `grounded.enqueue_purge_jobs`, is called by the worker's timer every 15 minutes, starting at worker start. The purge job itself, `grounded.purge_item`, is still a Procrastinate job, deferred by the sweep, and it is unchanged: one transaction per item, document row first.

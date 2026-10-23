# ADR 0027: Purge job for retired versions and deleted documents

**Status:** Accepted (Session 4, Step 7, PR 1). Built in PR 7.

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

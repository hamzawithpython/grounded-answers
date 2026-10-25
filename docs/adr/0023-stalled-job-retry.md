# ADR 0023: Stalled-job retry for ingestion versions

**Status:** Accepted (Session 4, Step 7, PR 1). Not built yet; built in PR 3.

**Date:** 2026-10-24

**Deciders:** Hamza Asif

## Context

ADR 0022 D5 leaves a gap. A worker killed hard leaves a version in `processing`, with no job to retry it, until its lease expires and someone enqueues a new job. This ADR closes the gap.

The source of truth is the version row: `processing_started_at` and the lease length (`WORKER_PROCESSING_LEASE_SECONDS`, ADR 0022 D1). The fencing token already decides which attempt may write.

Procrastinate 3.10.0 (the installed source, `procrastinate/manager.py` and `sql/queries.sql`) offers `get_stalled_jobs()` in two modes:

- **By start time:** jobs in `doing` for longer than `nb_seconds`.
- **By heartbeat:** jobs of a worker whose last heartbeat is older than `seconds_since_heartbeat` (default 30 s, the worker's `stalled_worker_timeout`).

Both work on jobs and workers, not on versions. Neither reads the lease that the ingest job enforces.

## Decision

1. A periodic sweep (`app.periodic`) calls a security-definer function `grounded.stalled_version_ids(lease_seconds)`. It returns **only** the tenant id and version id of each version in `processing` whose lease has expired. It returns no document data.
2. The sweep defers one ingest job per returned version, with `queueing_lock = "ingest:<version_id>"`. Procrastinate then allows only one waiting job per version. A duplicate defer raises `AlreadyEnqueued`, which the sweep catches and ignores. **Test:** two sweeps in a row produce one job.
3. The ingest job already takes over an expired lease (ADR 0022 D1). The sweep only enqueues. Two running attempts cannot both write, because of the fencing token.
4. The function is a **new security-definer function** (ADR 0023 is the first one in Step 7). It follows the rules in SECURITY.md: pinned `search_path`, no PUBLIC EXECUTE, a `NOLOGIN` owner role (`retry_definer`), and a test in `test_catalog`. It validates `lease_seconds` (120 to 7200) and raises on a bad value.

## Alternatives

| Option | Pros | Cons | Cost |
|---|---|---|---|
| **A. State-based sweep (chosen)** | The version row is the source of truth. The sweep and the fencing token use the same lease, so they agree. Ids only. | One more definer function to harden. | Medium |
| B. Procrastinate's `get_stalled_jobs` + `retry_job` | No new function. Built into 3.10. | Heartbeat mode sees a dead process, not a hung job in a live one. Start-time mode can retry a job that is still running. Neither knows the version's lease. | Low |
| C. Re-enqueue each claim with a delay | No scan. | A job that never runs is never re-enqueued. | Low, with gaps |

**Why A:** the lease is what the write path enforces. A retry rule based on the same clock cannot disagree with the fence. Option B is a good signal for worker liveness, but this decision needs the version's state, not the worker's.

## Consequences

- **Positive:** a killed worker's version is retried after the lease expires (default 20 minutes), without manual action. Repeated sweeps do not pile up jobs.
- **Negative:** a retried job may run twice if the first attempt is still alive. The fencing token makes the second write lose safely.
- **Negative:** one more definer function to review. It is covered by the catalog test.

## Tests (PR 3)

- The sweep returns the expired version, and not a live one or a fresh lease.
- Two sweeps produce one job (queueing lock).
- A killed attempt is retried by the sweep's job, and the version reaches `ready` exactly once.
- Cross-tenant: the function returns ids for every tenant, and the job runs in the version's tenant only.
- Catalog: pinned `search_path`, no PUBLIC EXECUTE, owner is `NOLOGIN`.

## Related

- ADR 0015 (Procrastinate), ADR 0017 (UUID-only arguments), ADR 0022 (lease, fencing, D5 gap).

## Amendment (PR 3, 2026-10-24): the defer runs inside the definer function

Decision 2 is changed, for two reasons found while building PR 3.

1. **The worker has no right to defer.** Migration 0005 grants enqueueing to app_user only. The original design had the worker defer the jobs, which would have needed a new grant on the queue tables. That grant is a wider privilege than the sweep needs.
2. **A batch defer stops at the first duplicate.** Procrastinate's defer SQL (`queue.procrastinate_defer_jobs_v1`, read from the installed 3.10.0 source) inserts a batch in one statement. A queued job with the same `queueing_lock` raises `unique_violation` for the whole statement. A sweep that deferred a batch would stop at the first version that already had a waiting job.

The change:

- The sweep is one definer function, `grounded.enqueue_stalled_ingest(lease_seconds)`, owned by `retry_definer`. It finds the expired leases and defers each version in its own block. A `unique_violation` counts that version as already waiting, and the loop continues. The job already waiting is not changed.
- `worker_user` has EXECUTE on that function, and no grant on the queue tables. It cannot enqueue any other way.
- The task name, `grounded.ingest_document`, is written in the function. A caller cannot choose another task.
- `retry_definer` gets the same grants as app_user for enqueueing (migration 0005), plus the event-row grant that the status trigger needs, and nothing more.
- The sweep does not use the Procrastinate App. It calls the function in one transaction and commits.

Verified by: a sweep where one stalled version already has a waiting job and another has none defers exactly one new job, and the existing job is the same row afterwards (`tests/db/test_stalled_retry.py::test_a_version_with_a_waiting_job_is_skipped_and_its_job_is_untouched`). With the per-version block removed, that test fails.

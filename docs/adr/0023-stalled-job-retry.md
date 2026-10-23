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

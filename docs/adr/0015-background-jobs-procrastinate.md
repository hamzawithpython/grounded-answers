# ADR 0015: Background Jobs with Procrastinate (PostgreSQL queue)

**Status:** Accepted

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

Session 4 needs background work: document ingestion (extract, chunk, embed, store) and the retention purge
(ADR 0007). Both must survive a restart, retry safely, and run on a schedule. The purge runs as
`worker_user`, and ingestion must start only when the document row it processes has committed.

The candidates were compared in the decision note (options table, 2026-10-22): Celery, Dramatiq, arq, RQ,
Taskiq and Procrastinate. Procrastinate was chosen because it stores jobs in PostgreSQL, so a job can be
inserted in the same transaction as the document row. A queue in Valkey cannot do that, and it leaves a gap in
which a committed document has no job, or a job exists for a document that was rolled back.

## Evidence (verified 2026-10-22)

- **Version:** Procrastinate 3.10.0, released 2026-09-23, MIT. Requires PostgreSQL 13+ (GitHub README).
- **Transactional enqueue, proved by a throwaway spike (not committed).** Inside an async SQLAlchemy session
  on the psycopg driver, the spike wrote a `documents` row and deferred a job on the same connection. It used
  `session.connection()`, then `get_raw_connection()`, then `driver_connection`, which is the psycopg
  `AsyncConnection`, and passed that to `defer_async(connection=...)`. A commit left 1 document row and 1 job row.
  A rollback left 0 and 0. The queue held 1 job in total. Procrastinate's docs describe the same pattern as the
  transactional outbox: <https://github.com/procrastinate-org/procrastinate/blob/main/docs/howto/production/external_connection.md>.
- **Schema:** `app.schema_manager.get_schema()` returns 599 lines of SQL for 4 tables (`procrastinate_jobs`,
  `procrastinate_periodic_defers`, `procrastinate_workers`, `procrastinate_events`), 18 functions, and 2 enum types. The functions have **no `SECURITY DEFINER`** and the schema has
  **no grants**.
- **Windows:** the upstream page says "Procrastinate has been manually tested and is known to work on Windows",
  with the limit that the worker cannot stop gracefully on SIGINT or SIGTERM. It says Windows "should primarily be
  used for development purposes only" (`docs/howto/basics/windows.md`).
- **Polling fallback:** the worker has `listen_notify` (default `True`) and polls for jobs every 5 seconds
  when it is off (`procrastinate/worker.py`).
- **Pooler mode:** `AsyncNullConnectionPool` disables pooling in the client, for use behind PgBouncer
  (`connector.md`).
- **Cleanup:** the built-in task `remove_old_jobs` deletes finished jobs older than a given age, and can run as
  a periodic task.
- **Failure recovery:** workers send heartbeats. A job left in `doing` by a worker that died is detected as
  stalled and can be retried (`docs/howto/production/retry_stalled_jobs.md`). Delivery is therefore
  **at least once**.

Not verified: Procrastinate's behaviour on the free tier of any host (depends on the host, chosen later);
psycopg_pool's default pool size (read from the constructor signature, which leaves `max_size` as `None`; to be
confirmed in Session 4).

## Decisions

### 1. Schema comes from Alembic, pinned to Procrastinate 3.10.0

- Migration `0005` (the next free number) contains Procrastinate 3.10.0's schema SQL, copied from
  `app.schema_manager.get_schema()` for that exact version. The version is pinned in `pyproject.toml`
  (`procrastinate==3.10.0`) and locked in `uv.lock`.
- A Procrastinate upgrade is a **new** Alembic migration. It carries the SQL difference for the new version and
  is never an edit to an applied migration (CLAUDE.md, migrations rule).
- The schema is applied by the migration role, like every other object. Procrastinate's own apply command is
  not used at runtime.

### 2. Grants, and the RLS boundary

- Every Procrastinate table and function is created and owned by `migration_role`. Following the rule in
  ADR 0004, `app_user` and `worker_user` get only what their code needs. The exact list is derived from each
  function's body in Session 4 and locked by the catalog test. The outline is:
  - `app_user`: `EXECUTE` on the defer functions (`procrastinate_defer_jobs_v1`, `procrastinate_defer_periodic_job_v2`),
    and `INSERT` on `procrastinate_jobs`. No `SELECT` on job rows (the defer function returns only the id).
  - `worker_user`: `EXECUTE` on the fetch, finish, retry, cancel, worker-registration and heartbeat functions,
    `SELECT` and `UPDATE` on `procrastinate_jobs` and `procrastinate_workers`, and `INSERT` on `procrastinate_events`.
  - Nobody else: `PUBLIC` has no privileges. `migration_role` owns everything.
- **RLS is not applied to the queue tables.** Procrastinate's tables have no tenant column, and adding one would
  change Procrastinate's schema and break upgrades. Tenant isolation for the data stays in the tenant tables
  (RLS, ADR 0004 and 0005). The queue tables carry only ids (decision 3). A worker can therefore see that a
  job for another tenant exists, through its id. The worker has the same kind of cross-tenant reach that
  ADR 0007 already accepts for `retention_due_tenant_ids()`. This is recorded as residual risk R17.
- The catalog guard (`test_catalog.py`, `expected_grants.json`) lists every grant on the queue tables and
  functions. A new grant fails CI until the list is reviewed.

### 3. Job arguments are ids only

- A job's arguments are UUIDs and a tenant id, and nothing else: no document content, no filename, no title,
  no email, no secret. The enqueue helper accepts typed id arguments only.
- A test enqueues every job type and reads back `procrastinate_jobs.args`. It fails if any value is not a UUID
  or is longer than the id format allows.

### 4. Delivery is at least once, so every job is idempotent

- A job can run more than once: after a worker crash, a stalled-job retry, or a retry after an error.
- **Ingestion** is idempotent by state. It runs only for a document version in `queued`, and it moves the
  version to `processing` in the same transaction that claims it. A second run finds the version not `queued`
  and does nothing. Writing chunks uses the version id, so a rerun replaces the same set.
- **Purge** is idempotent by design (ADR 0007, purge steps). Each step checks state first: `retired` to `purging`,
  delete the bytes (a missing object counts as deleted), delete the row. A rerun resumes from the state it finds.
- Each job type has a test that runs it twice and checks the result is the same as running it once.

### 5. Periodic cleanup of finished job rows

- The built-in `remove_old_jobs` task is scheduled periodically and deletes finished jobs older than a fixed age
  (initial value: 7 days), and the failed and cancelled ones too. The age is a setting with bounds.
- The job table stays small, and the purge and ingestion audit trail lives in `audit_log` (ADR 0007), not in the
  queue. Events are deleted with their jobs.

### 6. No LISTEN/NOTIFY through a transaction-mode pooler

- LISTEN/NOTIFY needs a session that stays connected. A transaction-mode pooler such as PgBouncer does not
  provide one.
- Procrastinate's worker therefore runs with `listen_notify=False` in any environment behind a transaction-mode
  pooler, and polls every 5 seconds. This adds up to 5 seconds of latency to a job start, which ingestion and
  the nightly purge tolerate.
- The host-selection checklist (ADR 0005, deployment checklist) gets a new line: check whether the host's pooler is
  transaction mode. If it is, set `pool_factory=AsyncNullConnectionPool` on the Procrastinate connector and
  `listen_notify=False` on the worker.

### 7. Connection budget

- A worker process uses a pool (`PsycopgConnector` with psycopg_pool; size to be confirmed, see Evidence) plus
  one dedicated connection for LISTEN when `listen_notify` is on.
- The web process keeps its SQLAlchemy pool (`pool_size=5`, `max_overflow=10`, ADR 0005).
- Before a host is chosen, the budget is: web maximum + worker pool + worker LISTEN + migration and operator
  connections, compared with the host's connection limit. A host whose free tier cannot fit the budget is not
  chosen. The numbers are recorded in the deployment checklist.

### 8. Windows development is limited to what upstream says

- Workers run on Windows only for development, as upstream recommends. Production runs on Linux.
- On Windows the worker cannot stop gracefully. A running job is halted, and at-least-once delivery (decision 4)
  makes that safe. The RUNBOOK says so, and the dev stop procedure is to end the worker process by its PID.
- Psycopg's async driver needs the selector event loop on Windows (ADR 0005). The worker starts with the same
  loop factory as the API.
- The app object is defined in a module, not in `__main__`. Procrastinate warns otherwise.

### 9. The worker treats the job's tenant id as untrusted

- A job carries a tenant id. The worker sets tenant context from it (`begin_worker_transaction`, the tenant
  transaction helper with no user), then reads the document by id under that context.
- Row-level security hides every row outside the context. So if the document is not found, either the id is
  wrong or the tenant id is wrong. In both cases the job is refused.
- **Refusal is a permanent failure.** The worker raises `DocumentNotFoundForTenant`, which is not in the task's
  `retry_exceptions` list (Procrastinate's retry strategy retries only the listed types, per its retry docs).
  The job is marked failed at once and is never retried.
- **The log line holds no ids and no content:** `ingest.refused reason=document_not_in_tenant`. The job id is
  recorded in Procrastinate's own job row, not in the log.
- **Session 4 test (planned):** enqueue an ingestion job for a document of tenant A with tenant B's id. Run the
  worker once. Assert: the document is unchanged, the job status is `failed`, the job ran exactly once (no retry),
  and the captured log holds neither the document id nor the tenant ids.

## Consequences

- **Positive:** a job cannot exist without the row that caused it, and a row cannot be committed without its job.
  No extra service: the queue lives in the database we already run.
- **Negative:** the queue load runs on PostgreSQL. Fine at this scale; to be measured in Session 4.
- **Negative:** the queue tables are cross-tenant by design, with ids only (R17).
- **Negative:** at-least-once delivery means every job type needs an idempotency test.
- **Negative:** Procrastinate has a smaller community than Celery (about 1.4k stars against 29k) and its Windows
  support is documented as development-only.

## Residual risks (new in this ADR)

- **R17, cross-tenant job rows.** A worker can read job rows for every tenant, containing ids only. Mitigation:
  no content in arguments (decision 3), grants limited to the worker's functions, and a test on the argument
  format. Accepted for v1.
- **R18, queue load on the database.** Polling and job writes use the same database as the tenants. Mitigation:
  measure in Session 4 and move the queue to its own database if needed (a new ADR).

## Alternatives considered

- **arq (Valkey):** simple and asyncio-native, but no transactional enqueue. Its last release was 2026-04-16.
- **Taskiq:** asyncio and young. Its broker options, and their maintenance, are not verified.
- **Celery:** does not support Windows (upstream docs), and has no transactional enqueue.
- **Dramatiq:** Redis and RabbitMQ only (upstream docs); no transactional enqueue.
- **RQ:** synchronous; each job would need `asyncio.run`, and no transactional enqueue.
- **Own outbox table with a poller:** would reimplement what Procrastinate provides, with more risk.

## Related decisions

- ADR 0004 (roles and grants; the worker's privileges)
- ADR 0005 (async SQLAlchemy and the tenant transaction; the connection budget)
- ADR 0006 (test database; the catalog guard)
- ADR 0007 (purge steps and their idempotency)
- ADR 0011 (`expect_rowcount`, `scope="function"`)

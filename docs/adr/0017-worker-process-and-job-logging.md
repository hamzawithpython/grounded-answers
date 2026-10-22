# ADR 0017: Worker Process, Worker Credentials, and Job Log Redaction

**Status:** Accepted. Session 4, Step 1b-i (worker, credentials, log filter, worker transaction) is built. Step 1b-ii (enqueue helper, poison-job rule) follows the decisions below.

**Date:** 2026-10-23

**Deciders:** Hamza Asif

## Context

ADR 0015 chose Procrastinate and its PostgreSQL queue. ADR 0016 placed the queue in its own schema. This ADR covers the worker process that runs the jobs, and the three places where job data could leak:

- the worker's login (`worker_user` can delete documents and their stored bytes, ADR 0007);
- the job's tenant context (a job has no user, only a tenant it works for);
- the logs (Procrastinate writes each job's arguments into its messages and into structured fields, and the arguments hold tenant and document ids).

## Decisions

### 1. The worker's login comes from its environment only (D1)

- `WorkerSettings` (`worker_config.py`) reads `WORKER_DATABASE_URL`. It sets `env_file=None`, so no `.env` file is read, not even one in the working directory.
- The web app's `Settings` never declares the key. A `.env` file that contains it fails startup, the same way the migration URL does (ADR 0004). Tests prove both.
- `.env.example` contains no worker value, and a test checks that.

### 2. The worker runs as `worker_user`, on the selector loop (ADR 0015 decision 8)

- Entry point: `uv run python -m grounded_answers.jobs.worker`, with `WORKER_DATABASE_URL` set in the shell.
- It uses the same selector loop factory as the API, because psycopg's async driver needs it on Windows.
- **Windows cannot stop the worker gracefully** (upstream limit). The dev stop is by process ID (RUNBOOK). Delivery is at least once and every job is idempotent, so a halted job is safe to run again.

### 3. A job has a tenant, not a user (`begin_worker_transaction`)

- `begin_worker_transaction` (in `tenant_db.py`, the only module allowed to set the context) sets `app.current_tenant` with `set_config(..., true)` as its first statement. It does not set `app.current_user`.
- It follows the same rules as `begin_tenant_transaction`: a new session per transaction, commit on success, rollback and re-raise on error. Tests prove that the context does not survive on the same pooled connection after a rollback or a commit.
- Only `create_tenant_user` reads `app.current_user`, and `worker_user` has no EXECUTE on it.

### 4. Job arguments are redacted in every log record, not only in the message (D-logging)

- Procrastinate writes arguments in three places: the message (`name[id](key=value, ...)`), the `call_string` field, and the `task_kwargs` field of `job`. It also writes them in the `jobs` list when a job is enqueued. The filter covers all of these, and any other extra field, at any depth.
- The filter (`jobs/logging_filter.py`) keeps the keys and drops the values. It keeps the job id, task name, status, timing and action, so every job's outcome is still logged. It never drops a record.
- The filter is installed on the **handler**, not on a logger, because a logger's filter does not apply to records from its child loggers (`procrastinate.worker`, `procrastinate.manager`, ...).
- Log level is not raised. The rule is to keep job start, success, failure and retry visible.
- **Limits, accepted for ids-only arguments:** a value that contains `key=` leaves that key name visible, and a value that contains `) ` ends the match early. ADR 0015 decision 3 allows only UUID arguments, and the enqueue helper (Step 1b-ii) enforces that, so neither case arises.
- **Open for Step 1b-ii:** the web app also enqueues, so its log handler must install the same filter. Procrastinate's `jobs` record is written there.

### 5. The worker's database role needs the prune grant (migration 0006)

- Every worker calls `prune_stalled_workers` at startup. Migration 0005 did not grant it, and a worker failed at startup with `permission denied for function procrastinate_prune_stalled_workers_v1`. Migration 0006 adds EXECUTE on that function and SELECT on `last_heartbeat`, which its `DELETE ... WHERE last_heartbeat < ...` reads.
- Both are reviewed in `expected_queue_grants.json`.

### 6. Procrastinate's attempts count

- `attempts` counts runs: Procrastinate's `finish` adds one. A job that ran once has `attempts = 1`. Tests assert that, and assert separately that the task body ran exactly once.

## Consequences

- **Positive:** the web app cannot load the worker's login, even by mistake, and tests prove it. The worker's logs keep every outcome and hold no tenant or document ids.
- **Positive:** the tenant helper set stays in one module, and a source test enforces it.
- **Negative:** the enqueue side (web app) must install the same filter, or its `Deferred` log line leaks the ids. This is a Step 1b-ii requirement.
- **Negative:** on Windows the worker has no graceful stop. That is accepted because of at-least-once delivery, and the RUNBOOK states the stop procedure.

## Related decisions

- ADR 0004 (migration URL, `.env` handling)
- ADR 0005 (`set_config`, the tenant transaction, the selector loop)
- ADR 0007 (`worker_user`, purge)
- ADR 0015 (Procrastinate, ids-only arguments, at-least-once delivery, Windows)
- ADR 0016 (queue schema, grants)

# ADR 0016: Queue Schema Placement and Worker Credentials

**Status:** Accepted (schema placement, Session 4 Step 1a). Worker settings: accepted, built in Step 1b.

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

ADR 0015 chose Procrastinate 3.10.0 and its schema is applied by migration `0005`. Two questions were open:

1. **Where the queue tables live.** Procrastinate's SQL names its tables and functions without a
   schema (`procrastinate_jobs`, `procrastinate_defer_jobs_v1(...)`). Whatever schema they land in
   must be on each role's `search_path`.
2. **Who holds the worker's database login.** `worker_user` can delete documents and their stored
   bytes (ADR 0007). Its credentials must not be readable by the web app.

Two facts shaped the choice:

- `migration_role` has no `CREATE` privilege on the database, so a migration cannot create a schema.
  The `grounded` schema is created by the admin bootstrap (`scripts/bootstrap_database.sql`) for this
  reason, and the queue schema has to be created the same way.
- The catalog guard requires every table in `grounded` to have RLS enabled and forced, with one
  documented exemption (`alembic_version`). Queue tables have no tenant column and no RLS
  (ADR 0015 decision 2), so putting them in `grounded` would need a second exemption.

## Options considered

| | A. Inside `grounded` | B. `public` schema | **C. Dedicated `queue` schema** |
|---|---|---|---|
| **Catalog guard** | Needs an RLS exemption for each queue table. | `public` has no `CREATE` for ordinary roles (bootstrap), so it cannot be used. | No change to the RLS rule. A separate grants rule for `queue`. |
| **Blast radius of a grant mistake** | Mixed with tenant tables. | Shared with every extension. | Limited to the queue objects. |
| **Bootstrap change** | None. | Would need a `CREATE` grant on `public`, which the bootstrap forbids. | One schema, owned by `migration_role`, created by the bootstrap. |
| **Upgrade path** | Procrastinate upgrades touch tenant schema. | Same. | Procrastinate upgrades stay inside `queue`. |

**Decision: C.** The guard's rule "every table in `grounded` has RLS forced" stays exemption-free.

## Decision

### 1. The `queue` schema

- The bootstrap creates `queue` with `AUTHORIZATION migration_role`, and checks the owner, as it does
  for `grounded`.
- Migration `0005` checks that the schema exists and is owned by `migration_role`, sets `search_path`
  to `queue` for its own transaction (`set_config(..., true)`), and runs Procrastinate's schema SQL.
- `app_user` and `worker_user` get `search_path = grounded, queue` (per database, in the bootstrap).
  `grounded` stays first, so tenant tables resolve exactly as before. Procrastinate's functions and
  triggers look up their names at run time, so the path must include `queue` for the caller's role.
- Procrastinate's own apply command is never used at run time (ADR 0015 decision 1).

### 2. Grants on `queue` are a separate reviewed list

- `tests/db/expected_queue_grants.json` lists every grant on `queue` objects (tables, sequences,
  functions, enum and composite types, and the schema). Any other grant fails the catalog test.
- `PUBLIC` has no privilege on any queue object. Postgres gives PUBLIC `EXECUTE` on new functions and
  `USAGE` on new types by default, so the migration revokes both.
- Each grant names the code path that needs it. The list is derived from the function bodies in
  Procrastinate 3.10.0's `schema.sql` and from the statements in `queries.sql`.

### 3. Worker credentials stay out of the web app

- The worker's login (`worker_user`, and its URL and password) is loaded by a separate `WorkerSettings`
  object, read only by the worker process. Its values come from the worker's environment or the shell,
  never from the web app's `.env`.
- The web app's `Settings` keeps `extra="forbid"`, and never contains a worker key. A test proves the
  web app refuses a worker URL key, the same pattern as the migration URL (ADR 0004, `test_env_example`).

## Consequences

- **Positive:** the queue can be upgraded, granted and audited without touching tenant objects. The
  web app cannot load the worker's credentials by accident.
- **Positive:** the bootstrap is the only place that creates schemas, so the owner rule stays in one
  file.
- **Negative:** an operator must re-run the database bootstrap on each existing database before
  migration `0005`, because the schema and the `search_path` are created there. RUNBOOK says so.
- **Negative:** Procrastinate's functions resolve names at run time. A role without `queue` on its
  path would fail with "relation does not exist". The connector test (`test_queue_connector.py`) proves
  both halves, and the `search_path` line is part of the bootstrap that is tested.

### 4. Enqueue grants for `app_user` (decided by the owner, Session 4 Step 1a)

Enqueue needs three grants beyond the obvious ones. A scratch test on the test database showed each
one fails in turn: the first error is "permission denied for table procrastinate_jobs", the second
"permission denied for table procrastinate_events", the third on the events sequence.

1. `SELECT (id)` on `queue.procrastinate_jobs`, for the `RETURNING id` clause of
   `procrastinate_defer_jobs_v1`. Only the id column. `app_user` cannot read `args`, `task_name`,
   `status` or any other column. **Accepted risk R17 (updated):** `app_user` can read every job's id
   across tenants. The ids reveal the overall job volume (how many jobs exist and how fast they grow),
   not what any job contains.
2. `INSERT (job_id, type, at)` on `queue.procrastinate_events`. The status trigger
   (`procrastinate_trigger_function_status_events_insert_v1`) writes one `deferred` event per job and
   runs as the caller. Procrastinate's triggers have no `SECURITY DEFINER`, and changing that would
   fork the upstream schema, so the grant is the smaller change. The web app can write event rows for
   any job id, but it cannot read them.
3. `USAGE` on `queue.procrastinate_events_id_seq`, the sequence that gives the event row its id.

## Licence: psycopg-pool (decided by the owner)

`psycopg-pool` 3.3.3 (pulled in by Procrastinate's `PsycopgConnector`) is **LGPL-3.0-only**. The
owner accepted it: psycopg is already LGPL (ADR 0005), and the library is used unmodified, so the LGPL
conditions on replacing the library are met by pip-installing the unchanged package. Procrastinate
itself is MIT.

## Related decisions

- ADR 0004 (roles, grants, the migration URL rule)
- ADR 0005 (psycopg, LGPL, unmodified use)
- ADR 0006 (catalog guard, test database)
- ADR 0007 (`worker_user`, purge)
- ADR 0015 (Procrastinate, job arguments, residual risks R17 and R18)

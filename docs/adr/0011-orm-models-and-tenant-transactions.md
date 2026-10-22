# ADR 0011: ORM Models, Schema Drift Check, and the Tenant Transaction Helper

**Status:** Accepted

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

ADR 0005 chose SQLAlchemy 2.1 for ordinary reads and writes, and explicit SQL for security objects
(policies, grants, functions). It set three rules: `lazy="raise"` on every relationship,
`deferred` for `password_hash`, and `set_config(..., true)` as the first statement of every tenant
transaction. Until this step none of them existed in code, so the leak risk (SECURITY T17, R8) and
the silent lazy-load risk (T18) were only designed.

## Decisions

### A. Relationships (decision A1)

Only two relationships are defined: `TenantMembership.user` and `User.memberships`. Both are
`lazy="raise"`. Every other foreign key is a column, not a relationship. A unit test fails if any
relationship is not `lazy="raise"`, so a future relationship cannot be added without the rule.

### B. Schema drift check (decision B1, strengthened)

- The models mirror the nine tables the migrations create. Every constraint and index has the
  name the migration gave it.
- An Alembic autogenerate comparison, against the migrated test database, must find no difference.
  It covers tables, columns, types, nullability, keys, unique constraints and indexes. Policies,
  grants, functions and triggers are not compared (ADR 0005).
- CHECK constraints are not in autogenerate, and they are not declared in the models either.
  `tests/db/expected_checks.json` holds every CHECK constraint's **name and definition**, as
  `pg_get_constraintdef` reports it. A changed rule under the same name fails the test.
- Verified by planting drift on purpose (nullability, a foreign key inside the documents and
  document_versions cycle, a renamed unique constraint). Each one was reported.

### C. Dependency before authentication (decision C1)

`TenantContext` (tenant id and user id) is defined now. `make_tenant_session(get_context)` builds
the FastAPI dependency; the function `get_context` comes from authentication in Session 3,
Step 3. In Step 2 the dependency is tested with a test-only context. No production route uses it.

### D. One session per transaction (decision D1)

`begin_tenant_transaction` opens a new session and transaction for each use. Its first two
statements are:

```sql
SELECT set_config('app.current_tenant', :tenant_id, true),
       set_config('app.current_user',   :user_id,   true)
```

Both values are bound parameters and transaction-local (`true`). Nothing else in `src/` may set
either setting; a unit test scans the source to enforce it.

## Additional decisions made in this step

### E. The FastAPI dependency uses `scope="function"`

FastAPI's default for a dependency with `yield` (`scope="request"`) runs the code after `yield`
after the response has been sent (FastAPI docs, "Dependencies with yield"). The commit is that code.
A failed commit would therefore reach the client as a success. With `scope="function"` FastAPI
finishes the dependency before the response is sent. A test shows a failed commit becomes a 500 and
stores nothing. A separate check confirmed that the default scope returns a 200 for the same failed
commit. `make_tenant_session` documents the requirement.

### F. Primary keys default to `uuid4` in the app; server defaults are returned

ADR 0005: ids are generated in the app. The primary key columns therefore carry a Python-side
`default=uuid.uuid4`, which adds nothing to the database schema. The mapper sets
`eager_defaults=True`, so INSERT returns `created_at` and the other server defaults. Without it,
those attributes are expired after a flush, and reading one needs a lazy load, which async code
cannot run.

### G. `users.password_hash` and `users.updated_at` are deferred

`app_user` has no SELECT on either column (migration 0002). Both are mapped `deferred=True`. A plain
`select(User)` therefore does not name them. An explicit `select(User.password_hash)` as `app_user`
is refused by the database, and a test checks that.

### H. Alembic does not silence application loggers

`alembic/env.py` called `fileConfig(...)` with its default `disable_existing_loggers=True`. Any
logger that already existed was disabled for the rest of the process. The rowcount test showed it:
the log line never reached the test. The call now passes `disable_existing_loggers=False`. This is
the migration environment, not a migration version, so the rule against editing applied migrations
does not apply.

## Rowcount (SECURITY R9)

`expect_rowcount(result, expected, operation)` raises `RowcountMismatchError` when the count is
wrong, and logs one line with the operation name and the two numbers. It never logs row data. No
application UPDATE or DELETE uses it yet, so every such write added later must call it.

## Consequences

- **Positive:** a missing or changed column, constraint or CHECK fails CI. A lazy-load mistake fails
  at the access, not later in production. A pooled connection cannot carry one tenant into the next
  transaction (proved on a single physical connection).
- **Negative:** the models and the migrations are two copies of the schema. The drift test keeps
  them aligned, but a new table needs a model, a row in the test and a name in the CHECK list.
- **Negative:** `expected_checks.json` is generated from the database once and reviewed by hand. It
  must be regenerated and re-reviewed whenever a CHECK changes.
- **Limit:** rollback and error tests do not detect a session-scoped `set_config`, because PostgreSQL
  reverts session settings set inside a rolled-back transaction. The commit test is the one that
  catches that bug (checked by mutation).

## Alternatives considered

- **Declare every CHECK in the models (B2):** rejected. Each rule would be written twice.
- **Compare only names of CHECK constraints (B2 variant):** rejected. A changed rule under the same
  name would pass.
- **Relationships for every foreign key (A2):** rejected for now. More surface and more lazy-load
  paths, with no current use.
- **Reuse the request session for the tenant transaction (D2):** rejected. A second `begin` on one
  session can keep state from the first.

## Related decisions

- ADR 0004 (roles, per-request checks, `app_user` column grants)
- ADR 0005 (SQLAlchemy, `set_config`, deferred columns, `lazy="raise"`)
- ADR 0006 (test database and logins used by the tests)

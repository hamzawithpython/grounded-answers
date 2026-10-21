# ADR 0005: Database Access Layer — Async SQLAlchemy 2.1 with psycopg 3, Alembic

**Status:** Accepted

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

SPEC open decision (database access) needs a choice before any tables are written. Constraints:

- **Streaming answers** (SPEC §7.3: p95 time to first token ≤ 3 s). The LLM, Telegram and HTTP calls are async. A sync database driver would hold a thread per open stream.
- **Row-Level Security** (ADR 0004): every transaction must carry the tenant context, and policies, `SECURITY DEFINER` functions and grants must be explicit SQL that we can review and test.
- **Two RLS exceptions** (ADR 0004): login and refresh lookups, both read-only functions owned by a `NOLOGIN` role.
- **Connection pooling** must not leak tenant context from one request to the next.

## Options considered

| | A. Sync SQLAlchemy + psycopg 3 + Alembic | **B. Async SQLAlchemy 2.1 + psycopg 3 (async mode) + Alembic** | C. Raw SQL with psycopg 3 + Alembic `op.execute` | D. SQLModel |
|---|---|---|---|---|
| **Concurrency** | One thread per in-flight request or stream. | Native async. Many open streams on one process. | Depends on chosen driver. | Same as the SQLAlchemy mode used. |
| **Fit with streaming and async LLM/Telegram calls** | Weak. | **Strong.** | Strong if async. | Same as B or A. |
| **RLS and explicit SQL** | Good. | Good: same SQL, executed with `text()` and `op.execute`. | Best control. | Thin maintenance (0.0.x). |
| **Pros** | Simplest. Most examples online. | Matches the rest of the stack. One driver for app and migrations. | Total control. | Fewest lines. |
| **Cons** | Blocks threads under load. Would be rewritten when streaming arrives. | More complexity: lazy-load pitfalls, session lifetime. | Boilerplate. Hand-mapped rows. | Same class for DB rows and API responses, so `password_hash` could leak into responses. **Rejected.** |

**Decision: B.** Async SQLAlchemy 2.1 with psycopg 3 in async mode. Starting sync and moving to async later would mean rewriting every route, dependency and test, so we pay the complexity once.

## Decision

### Libraries (verified 2026-10-22 on PyPI)

| Library | Locked | Released | License | Notes |
|---|---|---|---|---|
| `SQLAlchemy[asyncio]` | **2.1.5** | 2026-10-16 | MIT | Previous patch. 2.1.6 (2026-10-21) is one day old, so it is not adopted yet (see Dependabot cooldown). |
| `psycopg[binary]` | 3.3.6 | 2026-09-18 | **LGPL-3.0-only** | psycopg 3 in async mode. The `binary` extra bundles libpq. |
| `alembic` | 1.20.0 | 2026-09-11 | MIT | Migrations. Async `env.py` from the `async` template. |

- **`[asyncio]` extra is required.** In SQLAlchemy 2.1 the greenlet dependency no longer installs by default, so the extra must be declared explicitly ([2.1 changelog](https://docs.sqlalchemy.org/en/21/changelog/changelog_21.html)).
- **Why 2.1.5, not 2.1.6:** CLAUDE.md requires verifying versions, and a one-day-old release is not yet proven in the wild. We move to a new patch after a 7-day cooldown (Dependabot, below).
- **psycopg LGPL-3.0 note:** psycopg is licensed under LGPL-3.0-only. We use it unmodified as a library, which the LGPL allows. We don't modify psycopg. If we ever distribute binaries or modify psycopg, we re-check the LGPL obligations. Not legal advice; confirm with counsel before any distribution. The `binary` wheel also bundles libpq (LGPL), which is covered by the same review.
- **greenlet license** (pulled in by the `asyncio` extra): to be verified when we lock. Not verified in this ADR.

**Dependency rule (ADR 0002):** `pyproject.toml` uses `>=`:
- `sqlalchemy[asyncio]>=2.1.5`
- `psycopg[binary]>=3.3.6`
- `alembic>=1.20.0`

Exact versions and hashes live only in `uv.lock`. Dependabot proposes new patches after the cooldown.

### Engine and sessions

```python
engine = create_async_engine(
    settings.database_url,        # postgresql+psycopg://...  (scheme unchanged)
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,           # detect dropped connections before use
)
session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
```

- **`expire_on_commit=False`**: after COMMIT, attributes stay loaded. Without it, the next attribute access would trigger a lazy load, which fails in async code.
- **`lazy="raise"`** on every relationship. Accessing an unloaded relationship raises an error immediately. Without it, async code hits `MissingGreenlet` at a random point, or the code silently loads less data than expected. Tests cover this.
- **`deferred=True`** on `users.password_hash`, see "Column grants" below.
- **One `AsyncSession` per request**, created by a FastAPI dependency and closed when the request ends. Sessions are never shared between requests or stored globally.
- **Explicit transaction per request.** The dependency opens one transaction, sets the tenant context (below), runs the request, and commits on success or rolls back on any exception. Nothing commits implicitly.
- **Background jobs** (ingestion, evals) open their own session and set their own tenant per job. They never reuse a request session.

### Two dependency types

| Dependency | Used by | Transaction | Tenant context |
|---|---|---|---|
| `anon_session` | login, refresh, reset-token lookup | one transaction | None. Login and refresh use the two `SECURITY DEFINER` functions. Reset uses the tenant hint plus RLS (ADR 0004). |
| `tenant_session` | every authenticated endpoint | one transaction, opened after JWT verification | `set_config('app.current_tenant', :tid, true)` is the first statement, then the membership check (ADR 0004, per-request checks). |

### Tenant context: `set_config`, transaction-scoped, never plain `SET`

Every transaction that touches tenant data starts with:

```sql
SELECT set_config('app.current_tenant', :tid, true);
```

- **`true` = transaction-scoped** (`is_local`). The value is discarded at COMMIT or ROLLBACK.
- **Parameterized** (`:tid` is bound, not interpolated). `SET` cannot take bind parameters, and string interpolation would open an injection path. `set_config()` is a normal function, so it takes parameters safely.
- **Why plain `SET` leaks:** `SET app.current_tenant = ...` is session-scoped. The connection pool hands the same physical connection to the next request, which then inherits the previous tenant. Tenant A's request could set tenant A, and tenant B's request on that same connection would run with A's context if it forgot to set its own. Transaction scope removes that failure mode.
- **Fail closed:** a transaction that starts without `set_config` has no tenant. `current_setting('app.current_tenant', true)` returns NULL, and every policy evaluates to false, so zero rows.
- **Every transaction, not every request.** A request that commits and begins a second transaction must set the context again. The dependency provides a helper, `begin_tenant_transaction(session, tenant_id)`, as the only way to start a tenant transaction. We don't use an SQLAlchemy `after_begin` event, because setting the context there needs an awaited call, which is awkward with async sessions.

### ORM for CRUD, explicit SQL for security objects

- **ORM** (`DeclarativeBase`, `Mapped[...]` columns, UUID primary keys generated in the app with `uuid4`) for ordinary reads and writes.
- **Explicit SQL in Alembic migrations** (`op.execute`) for everything security-relevant:
  - `ENABLE` and `FORCE ROW LEVEL SECURITY` on each tenant table
  - `CREATE POLICY` (SELECT/INSERT/UPDATE/DELETE, scoped to `current_setting('app.current_tenant', true)`)
  - `SECURITY DEFINER` functions with `SET search_path = pg_catalog, <schema>` and schema-qualified names
  - `REVOKE` from `PUBLIC`, `GRANT` to `app_user`, column-level grants
  - The `auth_definer` role's narrow policies (ADR 0004)
- **Alembic autogenerate does not see RLS policies, functions or grants.** They're written by hand and tested. Step 6 asserts that `pg_policies` and `pg_proc` match the expected list, so a missing or extra policy fails CI.
- **ORM models mirror tables only**, never policies or functions.

### Column grants and `password_hash`

- `app_user` has `SELECT (id, email, display_name, active, created_at)` on `users`, **not** `password_hash`.
- The ORM `User` model maps `password_hash` with `deferred=True`. A plain `select(User)` therefore doesn't include it. Without this, every `select(User)` by `app_user` would fail with `permission denied`.
- The login function reads `password_hash` through `auth_login_lookup` only.
- Password changes use an explicit `UPDATE users SET password_hash = :h WHERE id = :id`. Column-level `UPDATE` without `SELECT` is allowed in PostgreSQL, so `app_user` can write the hash but never read it.
- Only two code paths write `password_hash`: change-password and reset-token. Both are tested.

### Migrations and roles

- Alembic uses the async template: `env.py` builds an `AsyncEngine` and calls `connection.run_sync(do_run_migrations)`.
- **Migrations connect with a separate URL, `MIGRATION_DATABASE_URL`**, using the migration role. `DATABASE_URL` is the runtime URL for `app_user`.
- **Scheme unchanged:** both URLs keep the `postgresql+psycopg://` scheme.
- **Current dev `DATABASE_URL` points at the Docker `postgres` superuser.** That changes in Step 4: runtime uses `app_user`, and migrations use the migration role. The superuser never runs the app.

### Windows event loop (psycopg async)

**Verified:** the [psycopg async docs](https://www.psycopg.org/psycopg3/docs/advanced/async.html) state: *"On Windows, Psycopg is not compatible with the default ProactorEventLoop. Please use a different loop, for instance the SelectorEventLoop."*

**Where it bites on this machine (verified 2026-10-22, Python 3.12.6, uvicorn 0.54.0):**
- `asyncio.run(...)` with no options gives `ProactorEventLoop` on Windows.
- uvicorn's `--loop auto` and `--loop asyncio` both return `ProactorEventLoop` on Windows when not using a subprocess (`uvicorn/loops/asyncio.py`).
- `uvicorn --reload` runs a subprocess, which is why it works by accident. Plain `uvicorn` does not.

**Fix (implemented in this step):**
- `grounded_answers/runtime.py` provides `selector_loop_factory()`, which returns `asyncio.SelectorEventLoop()`.
- uvicorn is started with `--loop grounded_answers.runtime:selector_loop_factory`. uvicorn accepts a custom import path for `--loop`.
- Async tests use the same factory through anyio's `anyio_backend` fixture: `("asyncio", {"loop_factory": selector_loop_factory})`. anyio 4.15.1 accepts `loop_factory` in `backend_options`.
- `ensure_psycopg_compatible_loop(loop)` raises `RuntimeError` with the fix in the message when the running loop is a Proactor loop. It runs where the database engine is created (Session 2, Step 3), so a wrong loop fails at startup, not on the first query.

**Scope:** Linux and macOS are unaffected. Their default loop is already a selector loop, and the factory returns the same type. CI runs on Linux, so CI doesn't exercise the Windows path. The Windows path is covered by the dev-machine check, plus the `runtime` tests, which run on every platform.

### Connection poolers and prepared statements (deployment caveat)

psycopg caches prepared statements automatically after `prepare_threshold` executions. The [psycopg prepare docs](https://www.psycopg.org/psycopg3/docs/advanced/prepare.html) say that connection poolers (PgBouncer and similar) are *"not compatible with prepared statements"* unless the middleware declares otherwise, because *"the same client connection may change the server session it refers to."* Since psycopg 3.2 PgBouncer support has caveats, and the docs advise disabling prepared statements when such middleware is used (`prepare_threshold = None`).

- **Rule:** when we choose a managed Postgres host, check whether its pooler runs in transaction mode. If it does, set `prepare_threshold=None` on the engine's connect arguments. This is a deployment setting, not a code change, so it's recorded here and in RUNBOOK.
- **Local Docker Postgres:** no pooler, so prepared statements stay on.
- **`set_config(..., true)` is pooler-safe.** In transaction pooling, the pooler assigns one server connection to a client for the duration of one transaction. `is_local = true` scopes the setting to that transaction, so it never outlives the transaction the pooler gives to this client. Session-level `SET` is the one that leaks in transaction pooling, because the next client can receive the same server session. The rule that keeps this safe is: *all* tenant queries for a request run inside the one transaction that starts with `set_config`.

### Dependabot cooldown

Add a cooldown so new releases wait before we adopt them. Security advisories are not delayed, because Dependabot's cooldown applies to version updates only (verified in the [Dependabot options reference](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference)).

Supported on **uv**, **npm**, and **github-actions** (the reference page's support table: pip and uv support `default-days` and the `semver-*-days` keys; npm and yarn support both; github-actions supports `default-days` only).

```yaml
  - package-ecosystem: uv
    cooldown:
      default-days: 7

  - package-ecosystem: npm
    cooldown:
      default-days: 7

  - package-ecosystem: github-actions
    cooldown:
      default-days: 7
```

- We use `default-days` only, for one rule per ecosystem. `semver-*-days` keys are optional and would only be added with a reason.
- Each block keeps its existing `directory`, `schedule`, grouping and ignore settings unchanged.

### Consequences

- **Positive:** streaming and async external calls fit naturally. One driver for the app and for migrations. RLS and security SQL are explicit and testable.
- **Positive:** `lazy="raise"` and `deferred` turn two classes of async bugs into loud errors, caught by tests.
- **Negative:** async code is harder to reason about than sync. We accept that for streaming.
- **Negative:** the ORM and explicit SQL coexist. Developers must know which objects are ORM-managed and which are migration-managed.
- **Negative:** `set_config` on every transaction is a rule we must enforce. Mitigation: a single helper, plus a test that a transaction without it sees zero rows.
- **Negative:** the `[asyncio]` extra and `binary` wheel add one more dependency each to manage.

## Resolved questions

1. **Two database URLs:** `DATABASE_URL` (runtime, `app_user`) and `MIGRATION_DATABASE_URL` (migration role). Accepted.
2. **`psycopg[binary]` for v1.** Accepted. Revisit system libpq at deployment for patch control.
3. **Pool sizes** (`pool_size=5`, `max_overflow=10`) are starting points, to be tuned with load tests later.

## Deployment checklist additions

- [ ] Choose the managed Postgres host. Check whether its pooler runs in transaction mode. If yes, set `prepare_threshold=None`.
- [ ] Confirm the bootstrap role work (ADR 0004) is allowed on that host.

## Alternatives considered

- **A (sync):** simplest, but would be rewritten once streaming arrives. Rejected.
- **C (raw SQL):** maximum control, but more boilerplate for every table. Rejected for CRUD. Its SQL style is kept for security objects.
- **D (SQLModel):** rejected, see the options table.
- **asyncpg instead of psycopg:** good performance, but its own API and no `set_config` difference that would help us. psycopg 3 supports the same async model with the standard DB-API, so we keep one driver.

## Implementation notes

- Dependencies to add in Session 2 (backend): `sqlalchemy[asyncio]`, `psycopg[binary]`, `alembic`. Locked via `uv lock`.
- Implemented now: `grounded_answers/runtime.py` (selector loop factory, loop check); uvicorn started with `--loop grounded_answers.runtime:selector_loop_factory` (RUNBOOK); anyio `backend_options` in `tests/conftest.py`; Dependabot cooldown on uv, npm and github-actions.
- `.env.example` (Step 4): `DATABASE_URL` (app_user) and `MIGRATION_DATABASE_URL` (migration role), placeholders only.
- Tests (Step 6, in addition to ADR 0004): a transaction without `set_config` returns zero rows; a pooled connection reused after tenant A's request has no tenant set for tenant B; `lazy="raise"` error raised for an unloaded relationship; `select(User)` by `app_user` succeeds without `password_hash`, and selecting it directly fails; `pg_policies` and `pg_proc` match the expected list.
- Dependabot: add the cooldown above, in a separate commit.

## Related decisions

- ADR 0001 Monorepo Layout · ADR 0002 Dependency Managers · ADR 0003 Valkey · ADR 0004 Authentication
- Pending: ADR 0006 (DB-dependent test strategy)

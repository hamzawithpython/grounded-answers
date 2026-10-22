# ADR 0006: Database Test Strategy — Service Container, Fresh Test Database, Real Logins

**Status:** Accepted

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

SPEC §7.1 requires tenant isolation to be *enforced in the database* and *proven by automated cross-tenant tests in CI*. ADR 0004 defines the roles and grants (`app_user`, `auth_definer`, the migration role). ADR 0005 defines the RLS policies, `SECURITY DEFINER` functions and `set_config` rules. None of that is proven until tests run against a real PostgreSQL with the real roles and migrations.

Requirements for the test setup:
- Tests run the same PostgreSQL version and pgvector extension as production (`pgvector/pgvector:0.8.7-pg17`, already pinned in `docker-compose.yml`).
- Tests run on every PR, so a broken setup can't hide a missing isolation test.
- Migrations are proven from an empty database on every run.
- Tests never touch the dev database.

## Options considered

| | **A. Service container, same image as dev** | B. Testcontainers (Python) | C. Embedded Postgres (`pgserver`, `pytest-postgresql`) | D. Hybrid: A plus optional B |
|---|---|---|---|---|
| **How** | GitHub Actions `services:` runs `pgvector/pgvector:0.8.7-pg17`. Locally the same image runs from docker-compose. Tests read `TEST_DATABASE_URL`. | Each session starts its own container via the Docker API. | Postgres binaries as a Python package, no Docker. | A in CI and locally; B for ad-hoc runs. |
| **Version parity** | Exact (same image tag). | Exact. | Risk: binaries and pgvector may differ from the pinned image. | Exact. |
| **New dependencies** | None. | `testcontainers` 4.15.0 (Apache-2.0, released 2026-07-24). Needs Docker on each dev machine and runner. | `pgserver` 0.1.4 was last released 2024-06 (stale). `pytest-postgresql` 9.1.1 is LGPL-3.0-or-later. | None for CI. |
| **Failure mode** | Clear: connection refused, so tests fail loudly. | Docker not running causes errors on dev machines. | Hard to diagnose version mismatches. | As A in CI. |
| **Effort** | Low. | Medium. | Medium, plus supply-chain review. | Low–medium. |

**Decision: A.** It gives production parity with no new dependency and no Docker-in-Docker. The RLS proof is the most important test set in the project, so it must run on every PR, on the same engine as production.

## Decision

### 1. Infrastructure

- **CI:** a GitHub Actions `services:` container, `pgvector/pgvector:0.8.7-pg17`, pinned by tag. Health check: `pg_isready`. Port 5432 on the runner.
- **Local:** the same image from `docker-compose.yml`, on `127.0.0.1:5432`, as today.
- **Test database:** `grounded_answers_test`. The dev database `grounded_answers` is never used by tests.
- **Test-only credentials:** the CI service container's password is the obviously fake value `ci-test-only-not-a-secret`, set in the workflow file. It is not a secret and can't reach anything outside the runner. If gitleaks flags it, we allowlist that **exact string** in `.gitleaks.toml` for that one file only, with a comment. No broad patterns.

### 2. Session setup (once per pytest run)

A session-scoped fixture `test_database` does this, in order:

1. **Safety guard** (below). Refuse to run if the guard fails. Nothing has been dropped at this point.
2. Connect as the **bootstrap superuser** (`TEST_ADMIN_DATABASE_URL`, to the `postgres` database). Only the test fixture and the superuser-bypass test use this connection.
3. `DROP DATABASE IF EXISTS grounded_answers_test WITH (FORCE)`, then `CREATE DATABASE grounded_answers_test`. `WITH (FORCE)` is available since PostgreSQL 13, which we are above.
4. Run the **cluster bootstrap** once per cluster, and the **database bootstrap** once per fresh database, both as the cluster admin, before migrations. Both are idempotent. Table, column and function grants are **not** in the bootstrap. They are Alembic migration `0001_roles_and_grants`, run as `migration_role` after the schema migrations (see the amendment below).
5. Run **`alembic upgrade head` from empty**, as the migration role (`MIGRATION_DATABASE_URL`). This proves the migrations work from scratch on every run.
6. Yield. Tests then connect as `app_user` via `TEST_DATABASE_URL`.

A round-trip test (`alembic downgrade base` then `upgrade head`) is added in Session 2, Step 4, to prove the migrations are reversible.

### 3. Safety guard

The `DROP DATABASE` is executed through **`TEST_ADMIN_DATABASE_URL`**, so the guard checks that URL as well as `TEST_DATABASE_URL`. Before anything is dropped, the fixture checks:

1. **Target name.** The database that will be dropped is exactly `grounded_answers_test`. This is a constant in the fixture, not read from any URL, so a URL can't redirect the drop.
2. **`TEST_DATABASE_URL`** names a database that is exactly `grounded_answers_test`.
3. **`TEST_ADMIN_DATABASE_URL`** connects to the maintenance database `postgres`. Its *own* database name must not be `grounded_answers` or any name in `DATABASE_URL` or `MIGRATION_DATABASE_URL`.
4. **Same server.** `TEST_DATABASE_URL` and `TEST_ADMIN_DATABASE_URL` have the same host and port.
5. **No dev URL reused.** `TEST_ADMIN_DATABASE_URL` and `TEST_DATABASE_URL` each differ from `DATABASE_URL` and `MIGRATION_DATABASE_URL`, compared as full URLs, so a dev URL can't be reused under a test name.

If any check fails, the run raises an error that names the failed check, and nothing is dropped. No connection is attempted before the checks pass.

**Tests for the guard** (`backend/tests/db/test_guard.py`, no database needed; each asserts the error and that no connection was made):
- `TEST_ADMIN_DATABASE_URL` pointing at the dev database name is refused.
- `TEST_ADMIN_DATABASE_URL` pointing at a different host or port than `TEST_DATABASE_URL` is refused.
- `TEST_DATABASE_URL` naming a non-`_test` database is refused.
- `TEST_ADMIN_DATABASE_URL` equal to `DATABASE_URL` (or `MIGRATION_DATABASE_URL`) is refused.
- A correct set of URLs passes.

### 4. Isolation tests use real logins, not `SET ROLE`

- Every isolation test connects as the **`app_user` login** (`TEST_DATABASE_URL`), through its own connection and engine.
- Two tenants are two separate connections. Tenant A's connection never executes a statement with tenant B's context.
- We do **not** use `SET ROLE app_user` from the superuser, because `SET ROLE` runs with the grants and RLS of the role, but it keeps the superuser's session, pool state and privileges. A real login proves the grants that production uses. This is the only way to show that `app_user` alone cannot read another tenant's rows.
- The superuser-bypass test is the single documented exception. It connects as the superuser to show that RLS does not apply to superusers, which is why the app must never use that login.

### 5. Data isolation between tests

Option (i) from the options review: **unique tenant IDs and unique emails per test, no truncation.**
- Each test creates its own tenants with `uuid4()` ids and emails that include a fresh `uuid4()`.
- Tests never assert on the total row count of a table, only on rows they created or on tenant-scoped results.
- The database is dropped at the start of the next session, so there is no cross-run leakage.
- This also allows parallel execution later, if needed, with no change to test data.

### 6. Fail, never skip

- If the database is unreachable, the `test_database` fixture **raises**. It does not skip. The error names the URL's host and port (never the password).
- DB tests are marked `@pytest.mark.db`. The CI job runs them unconditionally.
- A test that needs the database cannot call `pytest.skip`. Code review and a conftest check enforce this: the marker is only valid when the fixture is requested.
- Unit tests that don't need a database run without Docker. They are not marked `db`.

### 7. Environment variables

| Name | Used by | Who has it |
|---|---|---|
| `TEST_DATABASE_URL` | Tests, as `app_user` | CI, local `.env` |
| `TEST_ADMIN_DATABASE_URL` | Session fixture (drop/create, bootstrap), superuser-bypass test | CI, local `.env` only. Never in the app runtime. |
| `MIGRATION_DATABASE_URL` | Alembic migrations | CI, local `.env`, operator. Never in the web container. |

Values are never committed. `.env.example` lists the names with placeholders (Session 2, Step 4).

### 8. Scope and timing

- The DB test suite runs in a new CI job, `backend-db`, separate from the existing `backend` job so that unit tests stay fast and DB failures are clearly labeled.
- Expected runtime: not measured yet. We record the measured time in RUNBOOK after the first run.

## Consequences

- **Positive:** production parity. No new dependencies. Migrations are proven from empty on every run. Isolation is proven through the same logins production uses.
- **Positive:** the fail-never-skip rule means a broken CI service can't hide a missing isolation test.
- **Negative:** DB tests need Docker locally. Developers without Docker can run unit tests only, and CI runs the DB tests.
- **Negative:** dropping and recreating the database each run costs seconds per session. Acceptable at our scale; we'll measure it.
- **Negative:** test data accumulates within a session. Mitigated by unique ids, and the database is dropped at the next run.
- **Negative:** a bug in the bootstrap script shows up as a failing session setup, which looks like an infrastructure error. Mitigation: the fixture error names the failing step.

## Alternatives considered

- **B. Testcontainers:** self-contained and standard. Rejected for now: it adds a dependency and requires Docker to start a container per test session on every machine. We can add it later for ad-hoc tests (option D).
- **C. Embedded Postgres:** no Docker, but a stale package (`pgserver`, last released 2024-06) or an LGPL package (`pytest-postgresql`), and version or pgvector parity is uncertain. Rejected.
- **Truncate between tests:** clean state, but slower and it hides ordering bugs. Rejected in favor of unique ids.
- **`SET ROLE` from superuser:** convenient, but doesn't prove the real login's grants. Rejected for isolation tests.

## Implementation notes

- Dependencies: none new. `pytest`, `psycopg`, `sqlalchemy`, `alembic` are already planned.
- Files (Session 2, Steps 3–6): `backend/scripts/bootstrap_cluster.sql`, `backend/scripts/bootstrap_database.sql`, `backend/scripts/make_scram_verifier.py`, `backend/alembic/versions/0001_roles_and_grants.py`, `backend/tests/db/conftest.py` (session fixture, guard), `backend/tests/db/test_isolation.py`, `backend/tests/db/test_guard.py`, `backend/tests/db/test_catalog.py` (catalog guard).

## Amendment (2026-10-22): bootstrap split, grants as migrations

Accepted with these changes. This amends steps 2 and 4 above and the implementation notes.

**1. Three layers, each with its own owner and frequency**

| Layer | What | Who runs it | Frequency |
|---|---|---|---|
| Cluster bootstrap (`bootstrap_cluster.sql`) | Roles, role attributes, passwords (SCRAM verifiers), membership of the migration role in the definer roles | Cluster admin (local: Docker `postgres`; hosted: provider owner) | Once per cluster |
| Database bootstrap (`bootstrap_database.sql`) | `REVOKE CONNECT, TEMP ON DATABASE FROM PUBLIC`; `GRANT CONNECT` to `migration_role`, `app_user`, `worker_user`; `REVOKE ALL ON SCHEMA public FROM PUBLIC`; `CREATE SCHEMA IF NOT EXISTS grounded AUTHORIZATION migration_role`; per-database role `search_path` | Cluster admin (only the database owner or a superuser can grant `CONNECT`) | Once per database. The test fixture runs it for every fresh test database. |
| Migrations (Alembic) | Tables, RLS, functions, triggers, and every table, column and function grant (`0001_roles_and_grants`) | `migration_role` | Every upgrade |

Grants are migrations, so they are reviewed, versioned and re-applied on every fresh test database by `alembic upgrade head`.

**2. Passwords never reach the command line, the server log or the process list.**
- A helper, `make_scram_verifier.py`, reads the plaintext with `getpass` (no echo) and prints a **SCRAM-SHA-256 verifier**. The verifier is computed client-side: `SaltedPassword = PBKDF2-HMAC-SHA256(password, salt, 4096)`, `ClientKey = HMAC(SaltedPassword, "Client Key")`, `StoredKey = SHA-256(ClientKey)`, `ServerKey = HMAC(SaltedPassword, "Server Key")`, stored as `SCRAM-SHA-256$4096:<salt>$<StoredKey>:<ServerKey>`. PostgreSQL 17 stores a SCRAM string given to `ALTER ROLE ... PASSWORD` as-is, so the plaintext never leaves the client.
- The verifier is passed to psql as an environment variable. `bootstrap_cluster.sql` reads it with `\getenv`, not `-v`.
- Only the verifier reaches the server. Its statement can still appear in server logs if statement logging is on. Logging is checked per host (RUNBOOK).
- Passwords are ASCII only, so SASLprep normalisation can't change them.

**3. Running psql.** Locally: `docker compose exec` into the `postgres` service, so the psql client matches the server version (PostgreSQL 17). Environment variables are passed with `-e` and the script is piped on stdin (`-f -`). CI and the test fixture don't use Docker: the fixture computes verifiers in Python and runs the same statements through psycopg as the cluster admin.

**4. Membership of definer roles.** PostgreSQL 17's `GRANT` takes one option per `WITH` clause, so the membership is two statements, not one:

```sql
GRANT auth_definer TO migration_role WITH INHERIT FALSE;
GRANT auth_definer TO migration_role WITH SET TRUE;   -- keeps INHERIT FALSE (omitted options keep their value)
```

(The same for `retention_definer`.) `INHERIT FALSE` means the migration role doesn't pick up the definer roles' privileges automatically. `SET TRUE` lets it `SET ROLE` to them, which `ALTER FUNCTION ... OWNER TO` needs. Membership alone isn't enough, see point 5:

**5. The new owner needs `CREATE` on the function's schema for the moment of the change.** So the function migration does: `GRANT CREATE ON SCHEMA grounded TO auth_definer` → `ALTER FUNCTION ... OWNER TO auth_definer` → `REVOKE CREATE ON SCHEMA grounded FROM auth_definer`, all in one transaction. The definer roles never keep `CREATE`.

**6. Role search paths.** `app_user`, `worker_user` and `migration_role` get `search_path = grounded` per database (`ALTER ROLE ... IN DATABASE ... SET search_path`). Functions still pin their own `search_path` (ADR 0004).

**7. Catalog guard test.** `test_catalog.py` runs on every session and fails if:
- any table in schema `grounded` doesn't have RLS enabled **and** forced (`relrowsecurity` and `relforcerowsecurity`);
- the grants in `information_schema` don't match `tests/db/expected_grants.json`;
- any `SECURITY DEFINER` function in `grounded` lacks a pinned `search_path` (`proconfig`), or has `EXECUTE` for `PUBLIC` (`has_function_privilege('public', oid, 'EXECUTE')`).

This turns the RLS and grant design into something CI checks, not just something written in a document.
- CI: new job `backend-db` in `.github/workflows/ci.yml` with the service container and the three URLs. Pinned image tag, action SHAs pinned as per existing practice.
- RUNBOOK: how to start docker compose and run `pytest -m db`.

## Related decisions

- ADR 0001 Monorepo Layout · ADR 0002 Dependency Managers · ADR 0003 Valkey
- ADR 0004 Authentication (roles, grants, bootstrap) · ADR 0005 Database Access Layer (policies, `set_config`)

## Second amendment (2026-10-22): one bootstrap path, libpq verifiers, real logins

**1. Verifiers come from libpq, not from our code.** `grounded_answers/scram.py` calls `PGconn.encrypt_password(passwd, user, b"scram-sha-256")` in psycopg 3.3.6, which wraps libpq's `PQencryptPasswordConn`. The libpq docs say it takes a connection and uses the server's `password_encryption` only when no algorithm is given. Our code checks the input (ASCII, minimum 16 characters) and the output format. No hand-written PBKDF2 or SCRAM. The earlier hand-written derivation is removed.

**2. One bootstrap path, everywhere.** CI and the operator run the same `bootstrap_cluster.sql` and `bootstrap_database.sql` with psql. The test fixture pipes the same files to psql. psql meta-commands (`\getenv`, `\if`) don't work through a Python driver, so psycopg is used only for the statements that need it (drop and create the database, read `pg_authid`, compute verifiers).

**psql comes from the server image, not from the runner.** The GitHub ubuntu-24.04 readme lists a PostgreSQL 16.15 server and does not list a client. Rather than install an apt client (16 against our 17 server), the job runs psql inside the pinned `pgvector/pgvector:0.8.7-pg17` service container with `docker exec`. Version parity, and no apt step. The local equivalent is `docker compose exec`. Both are set through `PSQL_COMMAND`.

**3. Passwords are random per session.** The fixture generates one random password per login role, computes its verifier, and passes verifiers to psql through the environment (`-e` flags). The fixed CI password is now used only for the cluster admin of the CI service container. `TEST_DATABASE_URL` is no longer configured: the fixture derives the `app_user`, `worker_user` and `migration_role` URLs from `TEST_ADMIN_DATABASE_URL`.

**4. Login proves the verifier.** `test_login.py` logs in as each role over TCP with the matching password (real SCRAM), refuses a wrong password, and refuses the definer roles. `test_verifier.py` checks that the verifier stored in `pg_authid` equals the one libpq produced.

**5. Catalog guard** (`test_catalog.py`): role attributes, PUBLIC privileges on the database and `public` schema, schema ownership, RLS (enabled and forced) on every table, `SECURITY DEFINER` search_path pinning and PUBLIC EXECUTE, and grants against `expected_grants.json`. The RLS and function checks are vacuous until the first tables and functions exist. `expected_grants.json` is `[]`, so any grant added later fails until the list is updated in review.

**6. Migration naming.** The grants migration is the last migration in the sequence, after the tables, functions and triggers it grants on. The exact number is set when the migrations are written.

**7. Redaction.** The fixture's error messages replace every verifier with `<redacted>`. The `DbHandle` dataclass excludes its URLs and passwords from `repr`.

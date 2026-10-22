# ADR 0009: Readiness Checks for `GET /ready`

**Status:** Accepted

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

Liveness (`/health`) says the process runs. Readiness (`/ready`) says the app can serve requests. A
container orchestrator sends traffic only to ready instances. `/ready` is public, so it must not
leak infrastructure details or create load.

Requirements:
- PostgreSQL reachable as `app_user` (ADR 0004, ADR 0005).
- Valkey reachable (ADR 0003, ADR 0008).
- Schema at the migration head the code expects. A schema behind the code is not ready.
- Public 503 names only the failed dependency: `"database"`, `"cache"` or `"schema"`. No hostnames,
  error text or versions.
- Failure detail is logged server-side, without secrets or connection strings.

## Options considered

### How the expected schema head is known

| | **A. Constant in code, tested against migrations** | B. Read the head from the Alembic scripts at runtime | C. Only check that `alembic_version` has a row |
|---|---|---|---|
| Pros | Simple. No Alembic import in the app. | One source of truth. | Simplest. |
| Cons | Two places to edit. A test catches drift. | The runtime image ships `alembic/` and Alembic. | Misses a schema behind the code. Fails the requirement. |

**Decision: A.** `EXPECTED_SCHEMA_HEAD` in `readiness.py`. `tests/test_schema_head.py` fails when it
differs from the newest migration. The migration round-trip test uses the same constant.

## Decision

### Checks

- `database`: `SELECT 1` on a connection from the app engine (`app_user`).
- `cache`: `PING` on the Valkey client.
- `schema`: `SELECT version_num FROM grounded.alembic_version`, compared with the constant. This needs
  the SELECT grant from migration 0004.

Each check raises on failure. The checks are plain async functions, so unit tests inject fakes and the
real ones are tested against the test database and Valkey.

### Timeouts, budget, concurrency

- Each check: 1 s timeout (`asyncio.wait_for`).
- All checks together: 2 s budget. If it runs out, every check counts as failed.
- The checks run concurrently (`asyncio.gather`).
- If `database` fails, `schema` is not reported. It fails for the same reason, and the response should
  name the root cause only.

### Caching

The result, pass or fail, is cached for 2 s. Concurrent requests during a refresh wait on one lock and
share the result. A public endpoint must not open a connection per request, and a dead dependency must
not be hammered by retries.

### Response and logs

- `200 {"status": "ready"}`.
- `503 {"status": "not_ready", "failed": ["database"]}`. The list holds dependency names only.
- The log line has the check name and the exception class (`OperationalError`, `ConnectionError`,
  `TimeoutError`) or, for a schema mismatch, the two revision IDs. It never logs an exception message,
  because messages can contain hosts or credentials. Unexpected exception types are not caught, so they
  surface as a 500 and are logged by the server.

### Lifecycle

The lifespan creates the engine (after the ADR 0005 loop check) and the Valkey client at startup, and
disposes of the engine and closes the client at shutdown. Tests inject a `ReadinessChecker`, so they
open no connections.

## Consequences

- **Positive:** a dependency or schema problem shows up as 503 within about 2 s, and the public response
  reveals nothing useful to an attacker.
- **Positive:** the cache limits load on the database and Valkey.
- **Negative:** a recovered dependency can take up to 2 s to show as ready.
- **Negative:** the schema check needs `app_user` SELECT on `alembic_version` (migration 0004). This is
  the one catalog-guard exception, documented in `tests/db/test_catalog.py`.

## Residual risks

- A check that hangs inside the driver (below asyncio) could hold the lock until its timeout. The
  timeouts bound this.
- `/ready` is public. An attacker can still see 200 or 503 and the dependency names. That is accepted:
  the names reveal no secrets, and the endpoint is needed by the orchestrator.

## Related decisions

- ADR 0003 Valkey · ADR 0004 Authentication (`app_user` grants) · ADR 0005 Database access (engine and
  loop check) · ADR 0006 DB test strategy · ADR 0008 Valkey client

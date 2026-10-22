# Progress — Grounded Answers

**Last updated:** 2026-10-22 (Session 2, in progress: Steps 1–6 done, Steps 7–8 pending)

## Session 2 status at a glance

| Step | Work | State | Where |
|---|---|---|---|
| 1 | ESLint 9 → 10 | ✅ Merged | PR #8 |
| 2a | Auth decision (self-built JWT) | ✅ ADR 0004 Accepted, amended twice | PR #9 |
| 2b | DB access (async SQLAlchemy 2.1 + psycopg 3) | ✅ ADR 0005 Accepted | PR #10 |
| 2c | DB test strategy (service container, real logins) | ✅ ADR 0006 Accepted | PR #12 |
| 3 | Schema design (8 tables, composite FKs, ON DELETE RESTRICT) | ✅ `docs/ARCHITECTURE.md`, ADR 0007 | PR #12 |
| 4 | Roles and migrations (0001 tables, 0002 grants) | ✅ | PR #12 |
| 5 | RLS policies, definer functions, `create_tenant_user` (0003) | ✅ | PR #12, commit `b2192d0` (CI green) |
| 6 | Proof by tests | 🟡 Mostly done (see below) | working tree |
| 7 | `/ready` endpoint | ⬜ Not started | — |
| 8 | Docs: SECURITY.md, RUNBOOK, LEARNING_LOG, CHANGELOG | 🟡 Partly done | — |

## Step 6: what the tests prove (DB suite: 83 passing, 0 skipped)

Run with `pytest -m db` against `grounded_answers_test`. Each run drops and rebuilds that database.

- **Bootstrap:** the cluster and database SQL files run through psql, the same as the operator's commands. Verifiers come from libpq (`PGconn.encrypt_password`). Each login role authenticates with the password whose verifier was set.
- **Migrations:** the round-trip (upgrade, downgrade, upgrade) runs in its own database, `grounded_answers_test_roundtrip`, which is dropped afterwards. It is order-independent.
- **Catalog guard:** role attributes, PUBLIC privileges, schema owner, RLS enabled and forced on every table (`alembic_version` is the one documented exemption), `SECURITY DEFINER` search_path pinning and no PUBLIC EXECUTE, and an exact grants list (`expected_grants.json`).
- **Read isolation as app_user:** no tenant context gives zero rows; a tenant sees exactly its own rows, compared to the admin's ground truth.
- **Cross-tenant INSERT:** refused on every table, including `audit_log` with a NULL actor. The test records whether the grant or RLS refused it.
- **`create_tenant_user`:** success; seven refusal causes all give the same SQLSTATE and message; no row is created on refusal. The actor and tenant come from the session settings, never from parameters.
- **Worker purge transitions:** retired → purging only; delete only purging; a document can be deleted only once deleted and with no versions left; no cross-tenant change; no access to identity or token tables.
- **`updated_at` trigger** runs for app_user, and app_user cannot write `updated_at` itself.

Unit suite: 38 passing. Both suites run in CI (`backend` with `-m "not db"`, `backend-db` with `-m db`).

## Not done yet in Session 2

- **Step 7, `/ready`.** Checks PostgreSQL and Valkey with short timeouts. Returns 503 and names the failed dependency. Tests for healthy and failed cases.
- **Step 8, docs.**
  - `docs/SECURITY.md`: threat model; tenant-isolation controls; the residual risks in ADR 0004 (email squatting inside approved domains, admin enumeration, the defence-in-depth note on the actor check); backups keep deleted content after purge (ADR 0007).
  - `docs/RUNBOOK.md`: `/ready` and rollback steps; enabling branch protection (steps given in chat, not yet applied to `main`).
  - `docs/LEARNING_LOG.md`: this session's concepts and interview Q&A (RLS, `set_config` versus `SET`, `SECURITY DEFINER`, SCRAM, composite foreign keys, the silent REVOKE).
  - `CHANGELOG.md`: Session 2 entries are present; add the migrations and tests.

## Open items and decisions to remember

- **App auth code path is not built yet.** The login, refresh and logout endpoints, and the step that sets `app.current_user` and `app.current_tenant` together, come with the first endpoint that needs them. `create_tenant_user` has no caller in the app yet.
- **Branch protection on `main`** is not configured. Required checks: `backend`, `backend-db`, `frontend`, `security`. Add `backend-db` only after it has run on `main`.
- **Retention:** `DOCUMENT_RETENTION_DAYS` is in ADR 0007, but the setting is not in `Settings` yet. Add it with the purge job (Session 3).
- **Backups:** the backup retention window depends on the hosting provider. Record it in SECURITY.md when a host is chosen.
- **Managed Postgres:** when a host is chosen, check whether its pooler runs in transaction mode (set `prepare_threshold=None`) and whether it allows `CREATE ROLE` and `ALTER ... OWNER` (ADR 0005, ADR 0004).
- **Windows event loop:** the dev server must start with `--loop grounded_answers.runtime:selector_loop_factory` (RUNBOOK).
- **Dependency upgrades:** wait 7 days after a release before a manual upgrade (RUNBOOK). No date-based holds in `pyproject.toml`.

## Session 3 carry-over: check rowcount on every RLS-filtered write

- An UPDATE or DELETE that RLS filters out affects **0 rows and raises no error**. Only a `WITH CHECK` violation raises (`InsufficientPrivilege`, "row-level security").
- So the purge worker and every app write path must check `rowcount`. An unexpected 0 is a failure: log it and raise an alert. It is never treated as success.
- Tests required with the first write path that depends on this: the purge worker's transitions, and each app UPDATE or DELETE. The worker transition tests already assert the 0-row behaviour (`tests/db/test_worker_policies.py`). The logging and alerting, and the app-side checks, are not built yet.

## Next session starts here

1. Commit and merge the worker and trigger tests and this file (after approval).
2. Step 7: `/ready` (PostgreSQL and Valkey, short timeouts, 503 with the failed dependency, healthy and failed tests).
3. Step 8: SECURITY.md, RUNBOOK, LEARNING_LOG, CHANGELOG.
4. Then Session 3: retrieval and ingestion, starting with the document upload path and the purge job (with `DOCUMENT_RETENTION_DAYS`).

## Completed before Session 2

### Project setup
- ✅ Monorepo layout (backend/, frontend/) — ADR 0001
- ✅ Python dependency manager: uv — ADR 0002
- ✅ Node package manager: npm — part of ADR 0002
- ✅ In-memory cache: Valkey 9.1.2 (BSD license, open-source fork) — ADR 0003
- ✅ docker-compose.yml (PostgreSQL 17 + pgvector 0.8.7, Valkey 9.1.2)

### Backend (FastAPI, Python 3.12)
- ✅ Package structure, settings (pydantic-settings), `/health`, CORS allowlist (no `"*"`)
- ✅ Quality gates: ruff, mypy strict, pytest; CI job

### Frontend (Next.js 16, React 19, TypeScript)
- ✅ Scaffold, strict TypeScript, StatusCard, Vitest tests
- ✅ Quality gates: eslint (now v10), tsc, vitest, npm audit, build; CI job

### CI/CD and security
- ✅ `.github/workflows/ci.yml`: backend, backend-db, frontend, security (gitleaks)
- ✅ `.github/dependabot.yml`: weekly, with a 7-day cooldown on uv, npm and github-actions
- ✅ `.gitleaks.toml`, `docs/SECURITY.md` (known advisories)

## Local environment notes

- **Windows PostgreSQL:** stop any local PostgreSQL 17/18 service. Docker Compose provides the database on `127.0.0.1:5432`.
- **Node.js:** 24.15 or later (`.nvmrc`).
- **Backend:** run uvicorn with `--factory` and `--loop grounded_answers.runtime:selector_loop_factory`.
- **Database tests:** need Docker running and `TEST_ADMIN_DATABASE_URL` and `PSQL_COMMAND` set (RUNBOOK, "Running Database Tests").
- **Local `.env` files** are git-ignored. `CORS_ORIGINS` and `NEXT_PUBLIC_API_URL` must use `127.0.0.1`, not `localhost` (RUNBOOK, Dev Host Rule).

## Notes

- Tests are independent. None relies on a developer's `.env`.
- CORS is an allowlist with credentials and exact origins.
- Telemetry is disabled in the frontend.
- `uv.lock` and `package-lock.json` are committed.

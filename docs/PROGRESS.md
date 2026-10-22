# Progress — Grounded Answers

**Last updated:** 2026-10-22 (Session 4, Step 1a built on `feat/job-queue-foundation`, awaiting review)

## Session 3 complete (2026-10-22)

| Step | Work | State | PR |
|---|---|---|---|
| 1 | Admin CLI: tenants, domains, first admin, unlock (ADR 0010) | ✅ Merged | #16 |
| 2 | ORM models, tenant transaction helper, R8 tests, rowcount check (ADR 0011) | ✅ Merged | #17 |
| 3a | Keys, login, per-request checks (ADR 0012) | ✅ Merged | #18 |
| 3b | Refresh rotation, reuse, logout (ADR 0013) | ✅ Merged | #19 |
| 3c | Login limits in Valkey, timing floor (ADR 0014) | ✅ Merged | #20 |
| 4 | Background job runner decision: Procrastinate (ADR 0015) | ✅ Merged (`0e70fa9`) | #21 |

Open from Session 3: the `icacls` and HMAC key steps on your machine (RUNBOOK, Auth keys), and the Step 8 docs
from Session 2 (SECURITY, RUNBOOK, LEARNING_LOG, CHANGELOG). The R9 rowcount check is in place for the first app
UPDATE or DELETE.

## Session 4 status

Steps are split as you decided. Each step has its own branch and PR, and stops for review.

| Step | Work | State |
|---|---|---|
| 1a | Migration `0005` (Procrastinate 3.10.0 in schema `queue`), grants, catalog guard, connector and enqueue tests (ADR 0016) | 🟡 PR open on `feat/job-queue-foundation`, awaiting review. 119 unit and 182 DB tests pass. |
| 1b-i | `WorkerSettings` (env only), worker entry point as `worker_user`, `begin_worker_transaction`, redacting log filter, migration `0006` (prune grant) (ADR 0017) | 🟡 Built on `feat/worker-enqueue-1b`. Not committed, awaiting review. |
| 1b-ii | Enqueue helper with id-only arguments and task allowlist, rollback test, poison-job rule with a dummy task (ADR 0017) | Not started (after 1b-i is merged) |
| 2 | File storage decision, then implementation behind `storage_key` | Not started |
| 3 | Upload endpoint (tenant_admin, 10 MiB, magic bytes, sha256 dedupe, one transaction) | Not started |
| 4 to 8 | Extraction decision, chunks and embeddings, ingestion worker, replace/delete/purge, end-to-end with `demo-data/` | Not started |

**Decided in 1a (ADR 0016):** `app_user` gets `SELECT (id)` on `procrastinate_jobs`, `INSERT (job_id, type, at)` on `procrastinate_events` and `USAGE` on the events sequence, so enqueue works. R17 accepted: `app_user` reads job ids across tenants (ids only). `psycopg-pool` (LGPL-3.0-only, unmodified) accepted.

**Known open issue (not Session 4):** `tests/db/test_rate_limit.py::test_hard_lock_after_fifty_failures_and_operator_release`
failed once in three full DB runs. Being fixed in a separate PR before Step 1b (see "Next").

**Other 1a facts:** `EXPECTED_SCHEMA_HEAD` moved to `0005_procrastinate_schema`. The bootstrap (`scripts/bootstrap_database.sql`) creates schema `queue` and sets `search_path = grounded, queue` for `app_user` and `worker_user`. Existing dev databases must re-run the bootstrap (RUNBOOK) before `alembic upgrade head`.

## Session 4 plan (original, as written in Session 3)

1. **Migration 0005:** Procrastinate 3.10.0's schema, grants for `app_user` and `worker_user`, and the catalog
   guard entries (ADR 0015, decisions 1 and 2). The exact grant list comes from the function bodies. Upgrades are
   new migrations.
2. **Upload and ingestion:** upload endpoint (size and type checked from content, ADR 0007, SPEC §6.1), ingestion
   job with ids-only arguments, an idempotency test, and the tenant-mismatch test (ADR 0015, decision 9).
3. **Purge job** (ADR 0007): periodic task, idempotent steps, `DOCUMENT_RETENTION_DAYS` setting.
4. **Periodic cleanup** of finished jobs with `remove_old_jobs` (ADR 0015, decision 5).
5. **Host selection:** pooler mode (LISTEN/NOTIFY, ADR 0015 decision 6) and the connection budget (decision 7).

## Later improvements (not yet scheduled)

- **Separate `valkey-test` service in the test profile.** The test Valkey shares the dev Valkey container, on
  database 15, and the test fixture flushes that database. Postgres already has a separate `postgres-test`
  service on port 5433. Mirror that for Valkey, so tests never touch the dev instance's data (ADR 0006 pattern).

## Session 2 status at a glance (historical)

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
| 7 | `/ready` endpoint (redis-py 8.1.0, ADR 0008; checks and cache, ADR 0009; migration 0004) | ✅ Merged | `main` (PRs #14, #15; branch `feat/ready-endpoint` deleted) |
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

- **Step 8, docs.**
  - `docs/SECURITY.md`: threat model; tenant-isolation controls; the residual risks in ADR 0004 (email squatting inside approved domains, admin enumeration, the defence-in-depth note on the actor check); backups keep deleted content after purge (ADR 0007).
  - `docs/RUNBOOK.md`: `/ready` and rollback steps; enabling branch protection (steps given in chat, not yet applied to `main`).
  - `docs/LEARNING_LOG.md`: this session's concepts and interview Q&A (RLS, `set_config` versus `SET`, `SECURITY DEFINER`, SCRAM, composite foreign keys, the silent REVOKE).
  - `CHANGELOG.md`: Session 2 entries are present; add the migrations and tests.

## Open items and decisions to remember

- **App auth code path is not built yet.** The login, refresh and logout endpoints, and the step that sets `app.current_user` and `app.current_tenant` together, come with the first endpoint that needs them. `create_tenant_user` has no caller in the app yet.
- **Branch protection on `main`** is not configured. Required checks: `backend`, `backend-db`, `frontend`, `security`. Add `backend-db` only after it has run on `main`.
- **Retention:** `DOCUMENT_RETENTION_DAYS` is in ADR 0007, but the setting is not in `Settings` yet. Add it with the purge job (Session 4).
- **Backups:** the backup retention window depends on the hosting provider. Record it in SECURITY.md when a host is chosen.
- **Managed Postgres:** when a host is chosen, check whether its pooler runs in transaction mode (set `prepare_threshold=None`) and whether it allows `CREATE ROLE` and `ALTER ... OWNER` (ADR 0005, ADR 0004).
- **Windows event loop:** the dev server must start with `--loop grounded_answers.runtime:selector_loop_factory` (RUNBOOK).
- **Dependency upgrades:** wait 7 days after a release before a manual upgrade (RUNBOOK). No date-based holds in `pyproject.toml`.

## Session 3 plan: auth and foundations

Uploads need authentication, so Session 3 builds auth first. Ingestion and the purge job move to Session 4. SPEC.md is unchanged (§6.1 still lists ingestion as v1 scope).

1. ✅ **Admin CLI** (ADR 0004, ADR 0010; built on `feat/admin-cli`, awaiting review). Commands: create-tenant, add-domain, remove-domain, list-domains, create-admin. Click in the `operator` group; argon2-cffi for the first admin's password. Tests: unit 78 passing, DB 101 passing (incl. 11 for the CLI). `create-admin` checks the tenant and domain before the password prompt.
   - Manual check on the dev DB passed (create-tenant, add-domain, list-domains, create-admin with hidden prompts, mismatch refused, duplicate slug refused, wrong domain refused).
   - Dev tenant `indus-freight` (domain `indusfreight.example`, first admin `hr.manager@indusfreight.example`) now exists in the dev database.
   - `demo-data/` (Indus Freight Lines demo documents) is the owner's work. It is not in these commits; it goes on branch `data/demo-tenant` later. No migration added: migration 0003 already covers `migration_role` writes.
2. ✅ **ORM models, tenant transaction, R8, rowcount** (ADR 0005, ADR 0011; built on `feat/orm-models`, awaiting review). Nine models mirror the migrations (names checked by Alembic's comparison). Reviewed CHECK list in `tests/db/expected_checks.json` (name and definition). `begin_tenant_transaction` and `make_tenant_session` (dependency, `scope="function"`). R8 tests: pooled-connection leak on one physical connection (commit, rollback, error), statement order, `lazy="raise"`, deferred `password_hash`. `expect_rowcount` (R9). Tests: unit 85 passing, DB 116 passing. Mutation checks: planted drift and session-scope `set_config` are both caught. The admin CLI stays on explicit SQL (decision, not an oversight).
3. **Auth endpoints** (ADR 0004): login, refresh (rotation, reuse detection, 10 s grace), logout, logout-all, EdDSA keys with `kid`, lockout and rate limits in Valkey, generic errors with a dummy hash, audit entries, per-request membership and tenant-status checks. Tests for every rule in ADR 0004. Endpoint plan and key-management steps are reviewed before code.
4. **Background job runner ADR** (for Session 4): compare Celery, arq, Dramatiq and RQ. No implementation in Session 3.

Each step has its own branch and stops for review. Nothing is committed on `main`.

## Session 3 carry-over: check rowcount on every RLS-filtered write

- An UPDATE or DELETE that RLS filters out affects **0 rows and raises no error**. Only a `WITH CHECK` violation raises (`InsufficientPrivilege`, "row-level security").
- So the purge worker and every app write path must check `rowcount`. An unexpected 0 is a failure: log it and raise an alert. It is never treated as success.
- Tests required with the first write path that depends on this: the purge worker's transitions, and each app UPDATE or DELETE. The worker transition tests already assert the 0-row behaviour (`tests/db/test_worker_policies.py`).
- **Built in Session 3, Step 2:** `expect_rowcount` (logs one line with the operation name and counts, never row data; raises `RowcountMismatchError`), tested on an RLS-filtered update. No app UPDATE or DELETE uses it yet, so every app write added from Step 3 must call it.

## Next session starts here

1. Review the Step 1a PR (`feat/job-queue-foundation`). Do not merge until you approve.
2. Separate PR: fix the flaky rate-limit test (root cause and repeated-run proof).
3. Step 1b: worker, `WorkerSettings`, enqueue helper, poison-job test.
3. Before Session 4 ends: finish the Session 2 Step 8 docs, the `icacls` step on your machine, and the LEARNING_LOG entry for Session 4.

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

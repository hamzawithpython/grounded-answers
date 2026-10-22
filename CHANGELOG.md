# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Dependency and tooling changes are listed here too.

## [Unreleased]

### Documentation
- `docs/SECURITY.md`: threat model (assets, actors, trust boundaries, 24 threats with status and proving test),
  tenant-isolation layers, secrets table, backups after purge (ADR 0007), residual risks R1–R9.
- `docs/RUNBOOK.md`: health and readiness (ADR 0009); Session 2 bootstrap problems; rollback rule and
  per-revision table; branch protection with its verify command; restore procedure (planned with the purge job).
- `docs/LEARNING_LOG.md`: Session 2 concepts (RLS, FORCE, `set_config` vs `SET`, SECURITY DEFINER, SCRAM,
  composite FKs, silent REVOKE, liveness vs readiness) with interview Q&A.
- Repository made public. Branch protection on `main` applied and verified through the API (T24).

### Fixed
- `alembic/env.py` no longer silences existing loggers (`disable_existing_loggers=False`). The default
  disabled every app logger that existed when migrations ran in the same process (Session 3, Step 2).
- `backend/.env.example` no longer contains `MIGRATION_DATABASE_URL`. The web app loads that file, and
  `Settings` forbids unknown keys, so the app failed at startup. Migration credentials now come from the
  shell only (ADR 0004). `tests/test_env_example.py` fails CI if the example and `Settings` disagree.

### Added
- Session 3, Step 3b: refresh rotation with reuse detection, logout and logout-all (ADR 0013). The rotation
  takes a row lock (`SELECT ... FOR UPDATE`), and a race test proves that two concurrent refreshes with one
  token rotate once. Reuse after the 10-second grace window revokes the family. Family lifetime is capped at
  14 days from its first token. `POST /auth/refresh` and `POST /auth/logout` require an allowed Origin.
  `rowcount.affected_rows` added for writes that may legitimately affect zero rows.
- Session 3, Step 3a: authentication. `POST /auth/login` (generic 401, byte-identical bodies), `GET /auth/me`
  (per-request membership, user and tenant checks; role from the database). EdDSA access tokens (15 minutes,
  pinned algorithm, kid, required claims, 5-second leeway). Refresh token issued and stored hashed; cookie
  `__Secure-refresh` (HttpOnly, Secure, SameSite=Lax, Path=/auth). Argon2 verify in a worker thread. 422
  responses no longer echo input values. `scripts/make_jwt_keys.py` (key file, outside the repo). ADR 0012.
  Dependencies: `PyJWT[crypto]>=2.15.1` (MIT), `cryptography>=50.0.2` (Apache-2.0 OR BSD-3-Clause).
- Session 3, Step 2: ORM models for the nine tables (`models.py`), mirroring the migrations. Names checked
  by Alembic's comparison; CHECK names and definitions in `tests/db/expected_checks.json`.
- `tenant_db.py`: `begin_tenant_transaction` (the only way to start a tenant transaction; sets tenant and user
  with `set_config(..., true)` as its first statements) and `make_tenant_session` (FastAPI dependency,
  `scope="function"`, so the commit happens before the response). ADR 0011.
- `rowcount.py`: `expect_rowcount` for UPDATE and DELETE (SECURITY R9). Logs no row data.
- `database.py`: `create_session_factory` (`expire_on_commit=False`); the app lifespan sets `app.state.session_factory`.
- Tests: pooled-connection leak on one physical connection (R8), `lazy="raise"`, deferred columns,
  drift check, failed-commit behaviour, rowcount. Unit 85, DB 116 passing.
- Session 3, Step 1: admin CLI (`python -m grounded_answers.admin_cli`, run with `uv run --group operator`).
  Commands: `create-tenant`, `add-domain`, `remove-domain`, `list-domains`, `create-admin`. Uses
  `migration_role` from the shell only; every command is one transaction with an audit row (ADR 0010).
  `create-admin` checks the tenant and the email domain before it asks for a password.
- `grounded_answers/passwords.py`: password policy (12 to 256 characters, 10k common-password list) and
  Argon2id hashing with the pinned library defaults (ADR 0010). List vendored from SecLists (MIT), see `data/README.md`.
- Dependencies: `argon2-cffi>=25.1.0` (runtime, ADR 0004); `click>=8.5.0` in the `operator` group (ADR 0010).
  Transitive: `argon2-cffi-bindings`, `cffi`, `pycparser`. CI installs the `operator` group.
- ADR 0010: admin CLI (Click) and password hashing (argon2-cffi).
- `scripts/verify_login.py` (LOGIN OK per role) and `scripts/reset_role_password.sql` (one login role, allowlisted).
  Both are documented in the RUNBOOK.
- ADR 0004: authentication (self-built JWT in FastAPI; EdDSA access tokens; hashed, rotated refresh
  tokens; two read-only `SECURITY DEFINER` RLS exceptions; admin CLI for platform-owner work).
- ADR 0005: async SQLAlchemy 2.1 with psycopg 3 and Alembic (SQLAlchemy locked to 2.1.5).
- `docs/ARCHITECTURE.md`: data model (tables, composite FKs, `ON DELETE RESTRICT` everywhere), document
  lifecycle (retire, purge after `DOCUMENT_RETENTION_DAYS`, content-free audit), chunks deferred.
- ADR 0004 amendment: suspended tenants (`tenant_status` in login lookup; generic 401 at login, 403 per request).
- ADR 0006: DB test strategy (CI service container with the pinned pgvector image; fresh test database
  per session; migrations from empty; guard covering the admin URL; isolation tests use real logins).
- `grounded_answers/runtime.py`: selector event loop factory for psycopg async on Windows. uvicorn
  now runs with `--loop grounded_answers.runtime:selector_loop_factory` (RUNBOOK). Loop check for the
  database engine (added with the engine in Session 2, Step 3).
- Dependabot: 7-day cooldown on uv, npm and github-actions (version updates only).
- `GET /ready` (ADR 0009): checks PostgreSQL as app_user, Valkey PING and the schema head, concurrently,
  with per-check timeouts, an overall budget and a 2 s result cache. 503 names only the failed dependency.
- ADR 0008: `redis` (redis-py 8.1.0, MIT) as the Valkey client.
- Migration 0004: app_user can read `alembic_version.version_num` only (the one catalog-guard exception).
- `EXPECTED_SCHEMA_HEAD` with a test that ties it to Alembic's newest migration.
- CI: Valkey 9.1.2 service container in the `backend-db` job.

### Changed
- `backend/.env.example`: `DATABASE_URL` is now `app_user`; added `MIGRATION_DATABASE_URL` (migration role).
- Spec: SPEC §4 platform owner operations (create tenants, cross-tenant evals) run via an admin CLI using
  the migration role, not the web app (ADR 0004).
- Dev host is `127.0.0.1` for frontend and API (`next dev -H 127.0.0.1`, `CORS_ORIGINS` default, `.env.example`,
  RUNBOOK). Browsers treat `localhost` and `127.0.0.1` as different sites, so cookies would not be sent between them.
- CORS: `allow_methods` limited to `GET, POST, PUT, DELETE`; `allow_headers` limited to `Authorization, Content-Type`.
  Credentials stay enabled with exact origins only.

### Changed (ESLint, Session 2 Step 1)
- Frontend: upgraded `eslint` from 9.x to 10.13.0 (devDependency). Lint, typecheck, tests, prod audit,
  and build all pass. `eslint.config.mjs` needed no changes.
  Known gap: `eslint-plugin-react`, `eslint-plugin-jsx-a11y` and `eslint-plugin-import` (bundled by
  `eslint-config-next@16.4.2`) don't yet declare ESLint 10 peer support. npm reports them as invalid peers
  and installs anyway. Verified working via planted violations on 2026-10-22. Review date 2026-12-01.
  Documented in `docs/SECURITY.md`.

## [0.1.0] - Session 1

### Added
- Monorepo scaffold (backend FastAPI, frontend Next.js 16), Valkey cache, docker-compose, CI/CD, Dependabot,
  gitleaks. See `docs/PROGRESS.md` for the full list.

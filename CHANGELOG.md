# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Dependency and tooling changes are listed here too.

## [Unreleased]

### Fixed
- `backend/.env.example` no longer contains `MIGRATION_DATABASE_URL`. The web app loads that file, and
  `Settings` forbids unknown keys, so the app failed at startup. Migration credentials now come from the
  shell only (ADR 0004). `tests/test_env_example.py` fails CI if the example and `Settings` disagree.

### Added
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

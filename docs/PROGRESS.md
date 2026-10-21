# Progress — Grounded Answers

**Last updated:** 2026-10-22 (Session 2, Step 1 done)

## Completed

### Project Setup
- ✅ Monorepo layout (backend/, frontend/) — ADR 0001
- ✅ Python dependency manager: uv — ADR 0002
- ✅ Node package manager: npm — part of ADR 0002
- ✅ In-memory cache: Valkey 9.1.2 (BSD license, open-source fork) — ADR 0003
- ✅ docker-compose.yml (PostgreSQL 17 + pgvector 0.8.7, Valkey 9.1.2)

### Backend (FastAPI + Python 3.12)
- ✅ Package structure: src/grounded_answers/
- ✅ Settings class: pydantic-settings with PostgresDsn, RedisDsn, Literal types
- ✅ Configuration: environment-based, validated at startup, cached with @lru_cache
- ✅ /health endpoint (liveness probe, no external deps)
- ✅ CORS middleware with allowlist from settings (no `"*"`)
- ✅ Tests: 10 passing
  - Settings loading from env, validation errors, caching
  - Health endpoint, CORS headers (allowed/disallowed origins)
- ✅ Quality gates: ruff lint + format, mypy strict, pytest
- ✅ CI/CD: GitHub Actions job with all checks

### Frontend (Next.js 16 + React 19 + TypeScript)
- ✅ Scaffold with create-next-app (TypeScript, Tailwind, App Router, src/)
- ✅ Disabled Next.js telemetry
- ✅ TypeScript strict mode enabled
- ✅ npm scripts: dev, build, start, lint, typecheck, test
- ✅ StatusCard component: calls /health, shows online/offline/loading
- ✅ Tests: 4 passing (Vitest + React Testing Library)
  - Online, offline, loading states; fetch mocking
- ✅ Quality gates: eslint, tsc --noEmit, vitest, npm audit, npm build
- ✅ CI/CD: GitHub Actions job with all checks

### Documentation
- ✅ docs/SPEC.md (product requirements)
- ✅ docs/PROGRESS.md (this file)
- ✅ docs/LEARNING_LOG.md (key concepts, decisions, problems/solutions)
- ✅ docs/adr/0001-monorepo-layout.md
- ✅ docs/adr/0002-dependency-managers.md
- ✅ docs/adr/0003-valkey-cache.md

### CI/CD
- ✅ .github/workflows/ci.yml
  - Backend job: uv sync, ruff, mypy, pip-audit, pytest
  - Frontend job: npm ci, eslint, typecheck, vitest, **npm audit --omit=dev** (blocking), npm audit (non-blocking), npm build
  - Security job: gitleaks (full history scan) with custom allowlist for Docker image tags
  - Concurrency: cancel-in-progress on new push
  - Caching: uv wheels, npm packages
  - All actions pinned to commit SHAs (supply-chain security)
- ✅ .github/dependabot.yml
  - Three ecosystems: github-actions, npm (frontend), uv (backend)
  - Weekly schedule (Monday 03:00 UTC)
  - Grouping: minor + patch updates grouped, major updates separate
  - Ignores: semver-major updates for @types/node (must match Node 24 runtime)
  - Limit: max 5 open PRs per ecosystem
- ✅ .gitleaks.toml
  - Custom allowlist for Docker image version tags (7.4.11-alpine, 9.1.2-alpine3.24, etc.)
  - Extends default rules with [extend] useDefault = true
- ✅ docs/SECURITY.md
  - Threat model structure (TBD Session 2)
  - Known vulnerabilities: braces (GHSA-vfj7-8cjw-p6xm, no patch yet; dev-only mitigation via --omit=dev)
  - Security controls inventory: RLS, secrets, validation, CORS, rate-limiting, audit logging (TBD)
  - Pre-production checklist

### Code Quality & Testing
- ✅ Test isolation: autouse fixture in conftest.py (not duplicated in test_config.py)
  - Tests run without backend/.env (set via monkeypatch in conftest)
  - cache_clear() called before/after each test to prevent cache cross-talk
- ✅ Import-time side effects removed
  - Deleted module-level `app = create_app()` from main.py
  - Uvicorn now uses `--factory` flag to load app from create_app function
  - Prevents import-time DB/cache initialization (improves testing)
- ✅ All checks pass: ruff, mypy, pytest (10 backend), eslint, typecheck, vitest (4 frontend)

## In Progress
(None — all step (c) work done, Session 1 complete)

## Next Session (Session 2) Starts Here

### Priority Tasks
1. ✅ **ESLint 9 → 10 Upgrade** (done Session 2, Step 1: eslint 10.13.0; plugin peer gap tracked in SECURITY.md, review 2026-12-01)
   - ESLint 9 is end-of-support; upgrade to ESLint 10 immediately
   - Before upgrading: verify eslint-config-next supports ESLint 10 via official docs/changelog
   - Read ESLint 10 migration notes carefully
   - Upgrade deliberately, run all frontend checks (lint, typecheck, test, build)
   - Commit result with clear message

2. **Database Schema & Multi-Tenancy**
   - Create Alembic migration structure (migrations/ folder, env.py, versions/)
   - Database schema:
     - `tenants` table (id, name, created_at)
     - `users` table (id, tenant_id, email, role: admin/employee/viewer, created_at)
     - `documents` table (id, tenant_id, name, content_type, size, created_by, created_at, updated_at)
     - `chunks` table (id, document_id, content, embedding, metadata, created_at)
   - Create limited `app_user` PostgreSQL role (superusers/table owners bypass RLS)
   - Row-Level Security (RLS) policies:
     - `documents`: users see only their tenant's documents
     - `chunks`: users see only chunks from their tenant's documents
   - Add `/ready` endpoint: checks PostgreSQL + Valkey connectivity, returns 503 if unavailable
   - Write tenant isolation tests in CI: cross-tenant queries blocked
   - Run all checks, add integration tests for RLS
   - Commit with clear message

### Follow-on Tasks
- Retrieval & Ingestion (document upload, chunking, embeddings)
- Answering (LLM integration, prompt engineering, citations)
- Admin Features (document management, user management, analytics)
- Evaluation (eval dataset, metrics, dashboards)

## To Do (Session 3+)

### Retrieval & Ingestion
- Document upload: PDF/DOCX/TXT/Markdown with file size limits
- Text extraction: pypdf, python-docx, etc.
- Chunking: size, overlap, section-aware splitting
- Vector embeddings: pgvector in Postgres, embedding model (hosted or local)
- Hybrid retrieval: keyword search + vector search, reranking (optional)

### Answering
- LLM integration: Claude API (or other provider), with provider abstraction
- Prompt engineering: instructions, context, citation format
- Citation extraction: quoted spans from source documents
- Refusal on weak/missing evidence
- Conversation context (multi-turn follow-ups)

### Multi-Tenancy
- Tenant model in database
- Row-Level Security (RLS) in PostgreSQL
- Tenant isolation tests in CI

### Admin Features
- Document management: upload, replace, delete (soft/hard)
- User management: roles (admin, employee), per-tenant
- Analytics dashboard: questions/day, refusal rate, helpfulness, cost/answer
- Evaluation dashboard: eval results over time

### Readiness & Production
- /ready endpoint: checks DB, cache connectivity
- Health-check tests
- Error handling: explicit error messages, not bare except
- Logging: structured, no secrets, retention policy
- Secret management: .env (dev), environment variables (prod)

### Evaluation (Built-in)
- Evaluation dataset: questions, expected answers, source docs, unanswerable cases
- Eval runner: automated scoring on demand + in CI
- Metrics: recall@5, groundedness, citation accuracy, refusal accuracy
- Charts: results over time

### Channels (v1)
- Web chat (Next.js) — started (StatusCard shows API status)
- Telegram bot — pending
- Admin panel — pending

### Testing & Security
- Tenant isolation tests: cross-tenant queries blocked
- Prompt injection tests: document content treated as data
- Rate limiting: per user, per tenant
- Audit log: admin actions (upload, delete, user changes)
- Secret scanning: gitleaks in CI (done), pre-commit hooks (todo)

## Local Environment Notes

- **Windows PostgreSQL**: Stop local PostgreSQL 17/18 services and set to Manual startup (not Automatic). Docker Compose provides isolated services.
- **Node.js**: Requires Node >= 24.15 (jsdom 30 needs this). Currently running 24.20. Use `.nvmrc` to pin version.
- **Backend**: Run uvicorn with `--factory` flag (not module-level app instantiation).
- **npm audit**: Known advisory (braces, GHSA-vfj7-8cjw-p6xm, dev-only) documented in `docs/SECURITY.md`. CI gates on `--omit=dev`.

## Data Plan

- **Session 3**: Ingest Indus Freight Lines documents (mix of PDF and DOCX formats) into test environment.
- **Before Session 4**: Create ~50 synthetic evaluation questions covering:
  - Direct facts (shipper details, pricing, routing)
  - Comparison queries (rates across corridors, service levels)
  - Unanswerable questions (edge cases, missing data)
  - Multi-turn follow-ups
  These questions seed the eval dataset and CI evaluation runs.

## Notes
- All checks (backend + frontend) pass with zero warnings
- Tests are independent; no reliance on developer .env files
- CORS configured with allowlist (not wildcard)
- Telemetry disabled (frontend)
- uv.lock and package-lock.json committed for reproducibility

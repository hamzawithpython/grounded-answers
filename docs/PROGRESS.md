# Progress — Grounded Answers

**Last updated:** 2026-10-22 (end of Session 1)

## Completed

### Project Setup
- ✅ Monorepo layout (backend/, frontend/) — ADR 0001
- ✅ Python dependency manager: uv — ADR 0002
- ✅ Node package manager: npm — part of ADR 0002
- ✅ In-memory cache: Valkey 9.1.2 (BSD license, open-source fork) — ADR 0003
- ✅ docker-compose.yml (PostgreSQL 17 + pgvector 0.8.7, Valkey 7.4.11)

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
  - Frontend job: npm ci, eslint, typecheck, vitest, npm audit, npm build
  - Security job: gitleaks (full history scan)
  - Concurrency: cancel-in-progress on new push
  - Caching: uv wheels, npm packages

## In Progress
(None — all step (c) work done)

## To Do (Session 2+)

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

## Notes
- All checks (backend + frontend) pass with zero warnings
- Tests are independent; no reliance on developer .env files
- CORS configured with allowlist (not wildcard)
- Telemetry disabled (frontend)
- uv.lock and package-lock.json committed for reproducibility

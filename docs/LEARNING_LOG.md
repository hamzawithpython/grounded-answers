# Learning Log — Session 1 (2026-10-21)

## Overview
Built foundational project skeleton: monorepo with FastAPI backend, Next.js frontend, Docker services, GitHub Actions CI, and basic health-check integration.

## Key Decisions & Rationale

### 1. Monorepo vs Separate Repos
- **Decision:** Monorepo (backend/ and frontend/ folders)
- **Why:** End-to-end tenant-isolation testing in CI requires both stacks together; shared documentation and security specs; coordinated deployment
- **Trade-off:** One CI workflow coordinates two languages (Python + Node); manageable with GitHub Actions job matrices
- **ADR:** See `docs/adr/0001-monorepo-layout.md`

### 2. Python Dependency Manager: uv
- **Decision:** uv over pip+venv or Poetry
- **Why:** Fast, drop-in pip replacement, lock file (uv.lock) with hashes for reproducibility, minimal config (just pyproject.toml PEP 621)
- **Lock file behavior:** `uv add package` fetches current version with `>=` floor; `uv.lock` pins exact versions across platforms
- **Why not Poetry:** More complexity for a service app (publishing, TOML DSLs); uv is simpler
- **ADR:** See `docs/adr/0002-dependency-managers.md`

### 3. Node Package Manager: npm
- **Decision:** npm over pnpm
- **Why:** Only one JS package (frontend); pnpm's monorepo/speed benefits don't apply; npm is default, fewer tools to explain
- **Locking:** package-lock.json holds exact versions; `npm ci` for reproducible CI installs

### 4. In-Memory Cache: Valkey (not Redis)
- **Decision:** Valkey 9.1.2 (open-source fork of Redis 7.2.4 base)
- **Why:** BSD license (fully open source); Redis 7.4+ moved to RSALv2/SSPL/AGPLv3 (source-available, restrictive for deployment strategy)
- **Portfolio value:** Demonstrates licensing awareness; can explain in interviews why we chose the open-source fork
- **ADR:** See `docs/adr/0003-valkey-cache.md`

## Technical Concepts Learned

### Liveness vs Readiness Probes
- **Liveness (`/health`):** "Is the process running?" (no external dependencies)
  - Used by container orchestrators to restart dead processes
  - In our case: just returns `{"status": "ok"}`
- **Readiness (`/ready`, future):** "Can the app handle requests?" (checks DB, cache connectivity)
  - Used by load balancers to route traffic only to ready pods
- **Implementation:** Both are simple GET endpoints; liveness is always fast; readiness has latency

### Pydantic Settings for Configuration
- **What:** `BaseSettings` subclass that loads from environment variables and `.env` files
- **How:** Define fields with types (e.g., `database_url: PostgresDsn`); Pydantic validates on instantiation
- **If env var missing:** `ValidationError` raised at app startup (not at request time) — early failure is good
- **PostgresDsn / RedisDsn:** Validate URL format (scheme, host, port); reject malformed URLs
- **Literal types:** `Literal["debug", "info", ...]` enforces only specific string values
- **Why in code:** Better than ad-hoc getenv() calls; validation is centralized, testable, and fails fast

### FastAPI Dependency Injection
- **`@functools.lru_cache(maxsize=1)`:** In-process caching; Settings instance built once per process
- **`FastAPI.Depends()`:** Does NOT use lru_cache by default; evaluates dependency on every request
- **To actually cache:** Use lru_cache directly on the dependency function (as we did), or store in app state
- **Why:** Separates concerns; Settings can be tested in isolation; tests can clear cache with `.cache_clear()`

### CORS (Cross-Origin Resource Sharing)
- **Problem:** Browser enforces same-origin policy; requests from `http://localhost:3000` to `http://localhost:8000` are blocked without CORS headers
- **Solution:** Backend adds `CORSMiddleware` with allow-list of origins
- **Never allow `"*"`:** That means any origin can access; dangerous for APIs with state/auth
- **Headers sent:** `Access-Control-Allow-Origin: http://localhost:3000` (if origin is allowed)
- **Browser enforces:** Server can't force the browser to accept a response; CORS is a browser-side check
- **Implementation:** Allowlist from settings; default for local dev is both `127.0.0.1:3000` and `localhost:3000` (users might navigate to either)

### Next.js NEXT_PUBLIC_ Variables
- **What:** Variables prefixed `NEXT_PUBLIC_` are embedded in the browser bundle (visible in `window.__ENV__` or by inspecting the JS)
- **Use case:** Frontend config like API URLs, feature flags, public keys
- **Never store secrets:** API keys, tokens, credentials must NEVER use `NEXT_PUBLIC_` — they would be leaked to users
- **Why:** JavaScript in the browser is public; anyone can read it

### Testing with Vitest & React Testing Library
- **Vitest:** Jest-compatible test runner built on Vite (fast, ES modules by default)
- **React Testing Library:** Tests components by querying the DOM (e.g., `screen.getByText`), not by component state/internals
- **Mocking fetch:** Use `vi.stubGlobal('fetch', mockFn)` to intercept network calls
- **Async tests:** Use `waitFor()` to wait for async state updates; tests are deterministic only if you wait for side effects
- **Test file location:** Colocate `Component.test.tsx` next to `Component.tsx` for readability

### TypeScript Strict Mode
- **What:** `"strict": true` in tsconfig.json enables all strict checks at once
- **Covers:** `disallow_untyped_defs`, `noImplicitAny`, `strictNullChecks`, `strictPropertyInitialization`, etc.
- **Return types required:** Every function must have an explicit return type (not inferred)
- **Testing:** Make sure test type definitions are loaded (`types: ["vitest/globals"]`) so `expect()` is typed

## Code Practices Applied

### Monorepo Structure
```
backend/        # Python/FastAPI service
  src/grounded_answers/   # Package code (importable as `grounded_answers.*)
  tests/                  # Tests (using real Settings with _env_file=None)
  pyproject.toml          # uv config, dependencies, tool configs (ruff, mypy)
  uv.lock                 # Locked versions
  .env.example            # Template (git-tracked, user copies to .env)

frontend/       # Next.js/React app
  src/app/                # App Router
  src/components/         # Reusable components
  src/test/               # Test setup
  package.json            # npm config
  package-lock.json       # Locked versions
  tsconfig.json           # TypeScript config (strict: true, test files excluded from build)

docs/           # Documentation
  SPEC.md         # Product requirements
  PROGRESS.md     # What's done, what's next (updated at session end)
  LEARNING_LOG.md # This file
  SECURITY.md, RUNBOOK.md, EVAL.md, ARCHITECTURE.md  # (to be created)
  adr/            # Architecture Decision Records
```

### Tests Must Be Independent
- **Backend:** Autouse fixture sets env vars + clears `get_settings()` cache before/after each test
  - Tests use real `Settings` class (not mocks) with `_env_file=None`
  - Tests don't depend on developer's backend/.env (CI has none)
- **Frontend:** Mock `fetch` in each test; no filesystem dependencies
- **Why:** Parallel test runs; CI reproducibility; no environmental surprises

### CI/CD Pipeline
- **Single workflow (`ci.yml`) with parallel jobs:** backend, frontend, security
- **Backend job:** uv sync, ruff, mypy, pip-audit, pytest
- **Frontend job:** npm ci, eslint, tsc, npm test, npm build
- **Security job:** gitleaks (full git history, no `fetch-depth` truncation)
- **Caching:** npm cache-lock.json, uv wheel cache (faster reruns)
- **Timeouts:** 15 min per job (safety against hung tasks)
- **Concurrency:** Cancel in-progress runs on new push (save CI minutes)

## Problems Encountered & Solutions

### 1. `httpx` Deprecation Warning
- **Problem:** TestClient (Starlette 0.37+) moved from `httpx` to `httpx2`
- **Solution:** `uv add --dev httpx2` (replaces httpx)
- **Source:** https://github.com/encode/starlette/blob/master/starlette/testclient.py
- **Lesson:** Warnings are not optional; track upstream changes

### 2. Settings Fields Not Recognized by MyPy
- **Problem:** `Settings()` constructor (from pydantic-settings) not type-checked; fields seem missing at call time
- **Solution:** Enable pydantic mypy plugin (`plugins = ["pydantic.mypy"]` in pyproject.toml)
- **Lesson:** MyPy plugins extend understanding of framework magic; always read docs

### 3. TestSettings Copies in Tests
- **Original sin:** Test files created local `TestSettings` classes instead of using the real `Settings`
- **Problem:** If real Settings changes, tests don't catch regressions; not testing what production runs
- **Solution:** Use real `Settings(_env_file=None)` + monkeypatch for env vars
- **Lesson:** Test the real thing; mocking is for external dependencies (DB, API), not your own code

### 4. Create-Next-App Version Conflicts
- **Problem:** `@types/node@20` (default) vs Vitest's `@types/node@22+` requirement
- **Solution:** `npm install --save-dev "@types/node@^24"` (future-proof)
- **Lesson:** Monorepo dependencies can conflict; resolve by upgrading conservatively

### 5. `toBeInTheDocument()` Type Errors
- **Problem:** `@testing-library/jest-dom` matchers not recognized in TypeScript
- **Solutions:**
  1. Add `types: ["@testing-library/jest-dom"]` to tsconfig (matchers in globals)
  2. Exclude test files from Next.js build type check (`exclude: ["**/*.test.tsx"]`)
- **Lesson:** Test code has different type requirements; separate config for build vs tests

## Session Goals Status

✅ **Done:**
1. Repository layout (monorepo) + ADRs
2. Python dependency manager (uv) setup
3. Node package manager (npm) setup
4. Docker Compose (PostgreSQL + Valkey)
5. Backend scaffold (FastAPI, /health, tests, ruff, mypy)
6. Frontend scaffold (Next.js, TypeScript strict, ESLint, Tailwind)
7. Health status component (calls /health, shows online/offline)
8. CORS middleware (allowlist from settings, tested)
9. Vitest + React Testing Library (StatusCard tests, 4/4 passing)
10. GitHub Actions CI (ruff, mypy, pytest, eslint, typecheck, vitest, npm audit, gitleaks)

⏭️ **Next session (Session 2):**
1. Document ingestion pipeline (PDF/DOCX upload, text extraction, chunking)
2. Vector embeddings (pgvector, embedding model selection)
3. Retrieval: hybrid search (keyword + semantic)
4. LLM answering: prompt engineering, citations
5. Evaluation harness (eval dataset, metrics, automated CI runs)
6. Admin panel: upload documents, manage users
7. Readiness endpoint (`/ready` checking DB, cache)
8. Multi-tenancy: Row-Level Security in PostgreSQL

## Key Takeaways for Future Sessions

1. **Always verify versions online.** Training data has a cutoff (Feb 2025); Docker images, GitHub Actions, and tools update weekly. Check Docker Hub and GitHub releases before pinning.
2. **Lock files are not optional.** `uv.lock` and `package-lock.json` must be committed; `uv sync --locked` and `npm ci` enforce reproducibility in CI.
3. **Test the real thing.** If you create a test double, you're no longer testing production code.
4. **Settings validation at startup, not runtime.** Fail fast; pydantic-settings + strict types catch misconfigurations before the app listens.
5. **CORS is not negotiable.** A wildcard (`"*"`) is a security hole. Always use an allowlist; tie it to settings.
6. **Secrets never go in NEXT_PUBLIC_.** The browser sees it. Use server-side environment variables for secrets.
7. **Test files have different type config.** vitest/globals and testing-library types should be loaded; exclude test files from production build type check.

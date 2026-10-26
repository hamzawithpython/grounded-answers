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

## Late-Session Refinements (Session 1 Closure)

### Import-Time Side Effects & App Factory Pattern
- **Problem:** Module-level `app = create_app()` instantiated the app at import time, triggering DB/cache initialization
- **Impact:** Tests couldn't run without a real `.env` file; not testable in CI without infrastructure mocked
- **Solution:** Move to FastAPI app factory pattern:
  - Keep `create_app()` function; remove module-level instantiation
  - Run uvicorn with `--factory` flag: `uvicorn module:create_app --factory`
  - Tests instantiate fresh app per test: `app = create_app()` inside test function
- **Lesson:** Separate app construction from app startup; factory pattern is standard in production FastAPI services

### Test Isolation via conftest.py
- **Autouse fixtures:** Place shared test setup in `conftest.py` with `@pytest.fixture(autouse=True)`
- **Applies to all tests:** No need to pass fixture as parameter; runs before/after every test in the directory tree
- **Pattern:** Clear caches (`get_settings.cache_clear()`) before and after, set env vars via monkeypatch
- **Why it works:** Tests are now independent of developer's `.env` file; CI runs without `.env` present
- **Lesson:** Conftest is the right place for cross-cutting test setup; avoid duplication across test files

### npm Audit Gating Strategy for Monorepos
- **Problem:** npm audit reports dev vulnerabilities (braces, fast-glob, etc.) that don't reach production
- **Solution (2-step):**
  1. **Blocking gate:** `npm audit --omit=dev --audit-level=high` (fails CI if prod deps have high/critical vulnerabilities)
  2. **Non-blocking visibility:** `npm audit` with `continue-on-error: true` (reports all issues, doesn't fail CI)
- **Why:** Separates real risks (production dependencies) from dev-only risks (build tools, testing libs)
- **Documentation:** Record in SECURITY.md with rationale, review dates, and mitigation (dev-only nature)
- **Lesson:** Not all vulnerabilities are blocking; context (prod vs dev) determines severity

### Dependency Automation with Dependabot
- **Configuration:** Specify ecosystem (uv for Python, npm for Node, github-actions for Actions)
- **Grouping:** Use `update-types: ["minor", "patch"]` to group non-breaking updates; keep major updates separate for manual review
- **Ignores:** Can ignore specific major-version updates (e.g., @types/node must match Node runtime version)
- **Limits:** Set `open-pull-requests-limit` to control PR spam (e.g., 5 open at a time)
- **Lesson:** Automation reduces toil; configuration prevents noise; major updates always need human review

### Secret Scanning & Custom Allowlists
- **gitleaks:** Scans git history for credentials, API keys, etc.
- **False positives:** Docker image tags (7.4.11-alpine) can trigger detection
- **Solution:** Custom `.gitleaks.toml` with [extend] useDefault = true and regex allowlists
- **Pattern:** `'\d+\.\d+\.\d+-[a-z]+[\d.]*'` matches semantic versions with build tags
- **Allowlists MERGE:** Adding a custom allowlist doesn't replace defaults; both are checked
- **Lesson:** Secret scanning is mandatory in CI; false positives need explicit configuration, not disabled rules

## Key Takeaways for Future Sessions

1. **Always verify versions online.** Training data has a cutoff (Feb 2025); Docker images, GitHub Actions, and tools update weekly. Check Docker Hub and GitHub releases before pinning.
2. **Lock files are not optional.** `uv.lock` and `package-lock.json` must be committed; `uv sync --locked` and `npm ci` enforce reproducibility in CI.
3. **Test the real thing.** If you create a test double, you're no longer testing production code.
4. **Settings validation at startup, not runtime.** Fail fast; pydantic-settings + strict types catch misconfigurations before the app listens.
5. **CORS is not negotiable.** A wildcard (`"*"`) is a security hole. Always use an allowlist; tie it to settings.
6. **Secrets never go in NEXT_PUBLIC_.** The browser sees it. Use server-side environment variables for secrets.
7. **Test files have different type config.** vitest/globals and testing-library types should be loaded; exclude test files from production build type check.
8. **Avoid import-time side effects.** Use app factory pattern; instantiate at request time, not import time. FastAPI `--factory` flag + fresh app per test.
9. **Conftest.py is for cross-cutting setup.** Autouse fixtures here apply to all tests; clear caches before/after to prevent cross-talk.
10. **Not all vulnerabilities are equal.** npm audit: separate prod deps (blocking gate) from dev-only (non-blocking visibility). Context matters.
11. **Dependency automation needs guardrails.** Dependabot: group minor/patch (non-breaking), keep major updates separate, ignore version-pinned deps (e.g., @types/node per Node runtime).
12. **Document your security decisions.** SECURITY.md records vulnerabilities, mitigations, and review dates. Allowlists are not "ignoring" the problem; they're explicit trade-offs.

---

# Learning Log — Session 2 (2026-10-22)

## Overview
Built the data layer for multi-tenancy and the `/ready` endpoint. The main lesson: the database enforces tenant isolation, so a bug in the app cannot leak data by itself. Every control below is backed by a test that fails if the control is removed.

**Merged or open:** PR #8 (ESLint), #9 (auth design, ADR 0004), #10 (DB access, ADR 0005), #12 (schema, roles, RLS, migrations 0001–0003), #13 (`/ready`, ADR 0008 and 0009, test cluster, operator scripts, docs).

## Key Decisions & Rationale

### 1. Authentication: self-built JWT (ADR 0004)
- **Decision:** FastAPI verifies its own EdDSA tokens. No managed auth service.
- **Why:** Tenant context has to reach the database session. A third-party token format would need an extra mapping step on every request.
- **Trade-off:** we own the security. That is why ADR 0004 is long and has an amendment for each gap found.

### 2. Database access: async SQLAlchemy 2.1 + psycopg 3 + Alembic (ADR 0005)
- **Decision:** async, with explicit SQL for every security object.
- **Why:** streaming answers need async I/O. Alembic autogenerate cannot see policies, functions or grants, so those are written and tested by hand.

### 3. Test strategy: a real PostgreSQL per session (ADR 0006)
- **Decision:** a fresh `grounded_answers_test` database per run, with real login roles and random passwords. Tests fail rather than skip when the database is missing.
- **Why:** a test that skips when the database is down looks green in CI. A test that uses the superuser proves nothing about what the app can do.

### 4. Document lifecycle: soft delete, then purge (ADR 0007)
- **Decision:** delete retires content at once; a worker purges it after `DOCUMENT_RETENTION_DAYS`.
- **Limit (stated, not hidden):** backups keep deleted content until the provider's backup window ends. See SECURITY.md R3.

### 5. Valkey client: redis-py 8.1.0 (ADR 0008)
- **Decision:** redis-py, not valkey-py. Both are MIT. redis-py had more frequent releases in 2026, and ADR 0003 keeps the app code generic.

### 6. Readiness: checks, timeouts, cache (ADR 0009)
- **Decision:** three real checks, a 1 s timeout each, a 2 s budget, a 2 s result cache, and a public 503 that names only the failed dependency.

---

## Technical Concepts Learned

### Row-Level Security (RLS)
- **What:** PostgreSQL filters rows per query by a policy. The filter is a predicate on the table, so it applies to every query, however the app builds it.
- **How we use it:** each tenant table has policies that compare `tenant_id` with `current_setting('app.current_tenant', true)`. `USING` decides which rows a SELECT, UPDATE or DELETE can see. `WITH CHECK` decides which new rows an INSERT or UPDATE may write.
- **The silent case:** an UPDATE or DELETE that RLS filters out affects **0 rows and raises no error**. Only a `WITH CHECK` violation raises. So the app must check rowcount on every write (PROGRESS carry-over; tested in `test_worker_policies.py`).

**Interview Q&A**
- **Q: What does RLS protect against that a `WHERE tenant_id = ?` in the app does not?**
  A: A forgotten `WHERE` clause, a new query written by someone who did not know the rule, or a bug in a query builder. The database applies the predicate to every statement the role runs.
- **Q: What is the difference between `USING` and `WITH CHECK`?**
  A: `USING` filters which existing rows are visible to SELECT, UPDATE and DELETE. `WITH CHECK` validates rows being written (INSERT, and the new version of an UPDATE).
- **Q: Why does an UPDATE that RLS blocks not error?**
  A: From the database's point of view, the row is simply not visible. Only a write that would produce a row the policy rejects raises an error. The app must treat `rowcount == 0` as a failure.

### FORCE ROW LEVEL SECURITY
- **What:** by default, the table owner bypasses RLS. `FORCE` removes that exemption for the owner.
- **Why it matters here:** `migration_role` owns the tables. Without `FORCE`, a migration bug or a compromised migration login would see every tenant's rows.
- **Where it is tested:** `test_catalog.py` checks `relrowsecurity` and `relforcerowsecurity` on every table. `alembic_version` is the one documented exception: Alembic writes to it, and it holds no tenant data.
- **Also:** superusers and roles with `BYPASSRLS` ignore RLS entirely. `test_catalog.py` checks that no login role has either attribute.

**Interview Q&A**
- **Q: A table has RLS enabled. Who can still see every row?**
  A: The table owner (unless `FORCE` is set), superusers, and roles with `BYPASSRLS`.
- **Q: Why is the migration role, which owns the tables, not an exception?**
  A: Because it runs migrations, not the app. We force RLS so even a compromised migration login sees only what its policies allow. Migrations that need to bypass RLS would be a separate, reviewed step.

### `set_config` versus `SET`
- **`SET app.current_tenant = ...`** changes the setting for the whole session. A pooled connection keeps the session, so the next request that receives this connection inherits the tenant.
- **`SELECT set_config('app.current_tenant', :tid, true)`** sets it for the current transaction only. The third argument `true` ends the setting at COMMIT or ROLLBACK.
- **`SET` cannot take bind parameters.** `set_config` is a function, so the tenant ID is a bound parameter, not interpolated into SQL.
- **Fail closed:** `current_setting(name, true)` returns NULL when the setting is missing. The policies compare against NULL, so nothing matches.

**Interview Q&A**
- **Q: Why not `SET LOCAL`?**
  A: `SET LOCAL` also ends at the transaction, but it is a statement, not a function, so it cannot take a bound parameter. Using `set_config` avoids string building.
- **Q: What exactly leaks between requests with plain `SET`?**
  A: The connection pool returns the same server session to the next client. The setting from the last request is still there, so a request that forgets to set its own tenant runs as the previous tenant.
- **Q: Our tenant helper is not written yet. Why is that not a gap in the design?**
  A: It is designed and recorded (ADR 0005), and the leak test is required before the first tenant endpoint (R8). Until then no code path uses tenant data.

### SECURITY DEFINER hardening
- **What:** a function runs with the privileges of its owner, not the caller. That lets a narrow operation (for example, checking a login) run without granting the caller a broad privilege.
- **The risks and our controls:**
  1. **Search path hijack.** If the caller can create an object with the same name in a schema earlier in its search path, the definer runs that object. Control: `SET search_path = pg_catalog, grounded` on every definer function.
  2. **Unqualified names.** Control: every table and function referenced by its schema name.
  3. **PUBLIC execute.** PostgreSQL grants `EXECUTE` on functions to PUBLIC by default. Control: `REVOKE ALL ... FROM PUBLIC`, then a grant to the one role that needs it.
  4. **Owner can log in.** Control: the owner roles (`auth_definer`, `retention_definer`, `provisioning_definer`) are `NOLOGIN`, `NOBYPASSRLS`, `NOSUPERUSER`. `test_catalog.py` checks each.
  5. **Too many columns.** Control: each function returns only what its caller needs (for example, `retention_due_tenant_ids()` returns tenant IDs only).
- **Tested by:** `test_catalog.py` (`test_security_definer_functions_pin_search_path_and_are_not_public` and the exact grants list).

**Interview Q&A**
- **Q: Why does the search path matter for a definer function?**
  A: The function runs with the owner's rights, but name lookup uses the caller's search path. Without a pinned path, a caller can shadow a table or function and make the definer run the caller's code with the owner's rights.
- **Q: Why is the owner role `NOLOGIN`?**
  A: No one can connect as it, so there is no password to steal and no session to misuse. It only runs functions.

### SCRAM-SHA-256 verifiers
- **What:** PostgreSQL stores a password as a verifier: iterations, salt, a StoredKey and a ServerKey, all derived with PBKDF2. The server checks a login by challenge and response, and never receives the plaintext.
- **Why we compute it client-side:** `ALTER ROLE ... PASSWORD` with a plaintext sends the plaintext over the connection and can write it to the server log. We compute the verifier with libpq (`PQencryptPasswordConn`, exposed as `PGconn.encrypt_password`), then send only the verifier.
- **Our rules:** passwords are at least 16 ASCII characters (ASCII because PowerShell can change non-ASCII bytes on the way). Test verifiers are random per session. A verifier is still a secret: it is not printed, and it is redacted from `psql` error output.
- **Tested by:** `test_verifier.py` (format and input rules), `test_login.py` (the password whose verifier was set logs in, and the wrong one does not).

**Interview Q&A**
- **Q: Why is a SCRAM verifier safer than a hash of the password?**
  A: It is derived with a salt and a high iteration count, and the server never needs the password. Someone who steals the verifier still has to run PBKDF2 on each guess, and the login protocol cannot be replayed.
- **Q: Why compute the verifier on the client?**
  A: To keep the plaintext out of the connection and the server log. The server only stores what it receives.

### Composite foreign keys
- **What:** a foreign key over two columns, for example `(tenant_id, document_id) REFERENCES documents (tenant_id, id)`.
- **Why:** the referenced side needs a unique key on `(tenant_id, id)`. Then a row in tenant A cannot point at a row in tenant B, even if the app passes the wrong ID. The database refuses it.
- **ON DELETE RESTRICT everywhere:** a parent with children cannot be deleted, so no cascade can remove rows that the audit trail or the purge job still needs. The purge job deletes children first (ADR 0007).
- **Where:** `0001_tables.py`, every foreign key.

**Interview Q&A**
- **Q: What does a composite foreign key prevent that a single-column one does not?**
  A: A reference across tenants. A single-column key only checks that the ID exists, not that it belongs to the same tenant.
- **Q: Why `RESTRICT` and not `CASCADE`?**
  A: A cascade removes rows silently. Here, deleting a document must be an explicit, audited step, and the purge job controls the order.

### The silent REVOKE
- **What:** `REVOKE ALL ON TABLE t FROM role` does **not** remove column-level grants. Column privileges are stored separately from table privileges, so they stay.
- **Also:** revoking a privilege the role does not hold produces a **WARNING**, not an error. A migration that ignores warnings passes, and nothing changed.
- **Our fix:** the downgrade in `0002_roles_and_grants.py` names the columns explicitly. The catalog test (`test_grants_on_grounded_match_expected_list`) compares the whole grants list with `expected_grants.json`, so a leftover grant fails CI.

**Interview Q&A**
- **Q: You ran `REVOKE ALL ON users FROM app_user`. Can app_user still read a column?**
  A: Yes, if it had a column-level grant. Table-level REVOKE does not remove column grants. Revoke the columns by name too.
- **Q: How would you catch a grant that a migration forgot to remove?**
  A: A test that lists every grant from the catalog and compares it with an expected list, so any change needs a reviewed edit.

### Liveness vs readiness (Session 2 detail)
- **Liveness (`/health`):** the process is running. It checks nothing else. A failing liveness probe restarts the container.
- **Readiness (`/ready`):** the app can serve requests. It checks the database as `app_user`, Valkey, and the schema head. A failing readiness probe removes the instance from the load balancer, but does not restart it. A restart would not fix a database outage.
- **Design details (ADR 0009):** each check has a timeout; the checks run concurrently under a shared budget; the result is cached briefly, so a public endpoint does not open a connection per request; the public response names only the failed dependency.

**Interview Q&A**
- **Q: Why is a database outage a readiness failure and not a liveness failure?**
  A: Restarting the app does not fix the database. A liveness failure would restart every instance in a loop. Readiness removes the instances from traffic until the database returns.
- **Q: Why cache a readiness result?**
  A: The endpoint is public and the load balancer polls it. Without a cache, each poll opens database connections, and a slow database gets slower.
- **Q: Why not name the failing host in the 503?**
  A: The endpoint is public. A hostname or error message tells an attacker about the internal network. The log holds the detail.

### Migrations as explicit SQL
- **What:** each migration runs SQL we wrote, not SQL that Alembic generated from models.
- **Why:** autogenerate does not see RLS policies, definer functions or grants. A migration that the tests cannot read is a migration nobody reviews.
- **Rule:** an applied migration is never edited. A fix is a new migration (CLAUDE.md). Migration 0004 is the example: it grants one read to `app_user` on the schema version.

---

## Problems Encountered & Solutions

### 1. Dev passwords overwritten by the test fixture
- **Symptom:** after a DB test run, the dev `app_user` and `migration_role` passwords no longer worked.
- **Cause:** the test fixture runs `ALTER ROLE ... PASSWORD` on whatever cluster the admin URL points at, every session. The admin URL pointed at the dev cluster.
- **Fix:** a separate test cluster on port 5433 with its own volume and credentials, and a guard that refuses an admin URL on the dev host and port (`test_guard.py`).
- **Lesson:** a test that changes shared state is a deployment risk. Give tests their own state, and make the wrong target impossible, not just unlikely.

### 2. `MIGRATION_DATABASE_URL` in the web app's env file
- **Symptom:** `migration_database_url: Extra inputs are not permitted` at startup.
- **Cause:** the example file listed a migration credential. The app loads that file, and `Settings` forbids unknown keys.
- **Fix:** removed. Migration credentials are set in the shell only. `test_env_example.py` now fails CI if the example and `Settings` disagree.
- **Lesson:** a security rule ("migration credentials never reach the web app") should have a test. This one now does.

### 3. Silent variable not forwarded into a container
- **Symptom:** `ROLE_SCRAM is not set` from `psql`, even though PowerShell had set it.
- **Cause:** `docker compose exec -e NAME` forwards only the names it lists.
- **Fix:** name every variable the SQL reads, in `PSQL_COMMAND`, the RUNBOOK and `ci.yml`.

### 4. The schema check and an empty database
- **Symptom:** `/ready` returned 503 `schema` on a fresh database.
- **Cause:** `grounded.alembic_version` did not exist. The log showed `ProgrammingError`.
- **Fix:** none needed: the check was correct. It reports "not ready" until the migrations run. The RUNBOOK troubleshooting entry explains it.

---

## Key Takeaways for Session 3

1. Every write path checks rowcount, and treats 0 rows as a failure.
2. The tenant helper (`set_config`, transaction-scoped) and the pooled-connection leak test come with the first tenant endpoint (R8).
3. `lazy="raise"` is set with the first ORM model, with its test.
4. The purge job needs the restore procedure in the RUNBOOK before it runs in production.
5. Before any test writes to a database, ask: which cluster does this URL point at?

---

# Session 3, Step 1: Admin CLI (2026-10-22)

## Concepts in plain words

### CLI (command-line interface)
A program you run from a terminal with commands and options, such as `create-tenant`. The admin CLI is how the platform owner makes changes that the web app is not allowed to make. ADR 0010 explains why it uses Click.

### Transitive dependency
A package that you do not list yourself, but that one of your packages needs. Click is one: `uvicorn` needs it, so it is installed even though we did not ask for it. Declaring it ourselves (`click>=8.5.0` in the `operator` group) makes the dependency visible and tested in CI. It does not remove Click from the runtime.

### Dependency group
A named list of extra packages in `pyproject.toml`, installed only when asked for (`uv sync --group operator`). It is a way to say "this tool needs these packages" without adding them to the app.

### `getpass`
A standard-library function that asks for input without showing it on screen. It is how the CLI reads the password. A password typed as a command argument would be stored in shell history and visible in the process list, so the CLI never takes one that way.

### Argon2id
A password-hashing algorithm built to be slow and memory-hungry, so guessing is expensive. "id" is the variant that resists both side-channel and GPU attacks. A hash string holds the algorithm, its parameters, the salt and the result, so the parameters can change later without breaking old hashes.

### RFC 9106 profile
The IETF's recommended Argon2 settings. argon2-cffi's default is the second recommended profile: 64 MiB of memory, 3 passes, 4 threads. We pin those values in a test so a library upgrade cannot change them silently.

### Rowcount, one more time
A `DELETE` that matches no row is not an error in SQL. It returns zero rows. `remove-domain` uses `RETURNING` and refuses when nothing came back, so "nothing deleted" is never reported as success.

## Problems Encountered & Solutions

### 5. A DB test silently ran nothing
- **What happened:** the new DB test file ran "11 deselected", with no tests run.
- **Why:** the `db` marker was missing from the file. `pytest -m db` filters on that marker.
- **Fix:** `pytestmark = pytest.mark.db` at the top of each DB test file. A file with DB tests and no marker is a silent gap, so check the count of selected tests after every run.

### 6. Two URL schemes in one test
- **What happened:** `psycopg.connect()` rejected `postgresql+psycopg://...`.
- **Why:** SQLAlchemy URLs carry a driver suffix. Plain psycopg wants `postgresql://`.
- **Fix:** the CLI passes SQLAlchemy URLs; the test's direct psycopg connections use the plain form. Each tool gets the URL format it expects.

### 7. Ruff flagged test passwords as hardcoded secrets (S105)
- **What happened:** literal strings assigned to `GOOD_PASSWORD` and similar names were reported.
- **Why:** the rule matches on variable names, and the test values really are literal text.
- **Fix:** test passwords come from `secrets.token_urlsafe()` at run time. No literal password is in the repo, and the policy still applies to them. The security check was not disabled.

### 8. mypy could not see attributes re-exported by a module
- **What happened:** `admin_cli.getpass` and `admin_cli.admin_service` were reported as not exported.
- **Why:** strict mypy does not treat a module's imported names as its public attributes.
- **Fix:** tests import the module they patch (`import getpass`, `from grounded_answers import admin_service`) and patch that module.

## Interview Q&A

**Q: Why not put the admin CLI in the web app?**
A: The web app connects as `app_user`, which cannot create tenants or edit the allowlist. Giving the app the migration role would let any bug in the web code make platform changes. A separate CLI with its own credential keeps those powers out of the request path (ADR 0004).

**Q: The CLI and the web app both use Click or FastAPI's dependencies. Is the separation real?**
A: Partly, and it is important to say which part. Click is installed in the runtime environment through uvicorn, so the package is present in both. The separation is in the code and the credential: the CLI module is never imported by the app, and the migration URL is never in the app's environment or its `.env`. ADR 0010 states this.

**Q: Why is the password hashed in the CLI and not in the database?**
A: The database never sees the plaintext. The CLI hashes it with Argon2id before the insert, so the stored value is a hash. The web app's `app_user` cannot read the column at all (`permission denied`, tested).

**Q: What does "salted" mean in an Argon2 hash?**
A: A random value added to the password before hashing. Two users with the same password get different hashes, so an attacker cannot use one precomputed table for everyone. Argon2 stores the salt inside the hash string.

**Q: Why refuse common passwords when the length rule already exists?**
A: Length alone accepts `passwordpassword`. The 10,000-entry list catches the patterns attackers try first. We skip composition rules (must contain a digit), which push people toward predictable substitutions like `Password1!`.

**Q: What is the risk of an operator action with no actor?**
A: We cannot say which person did it. With one operator that is acceptable, and it is recorded as residual risk R10. Before a second operator joins, the audit row needs an identity.

---

# Session 3, Step 2: ORM models and the tenant transaction (2026-10-22)

## Concepts in plain words

### ORM model
A Python class that stands for a table. Columns are attributes, and rows are objects. The models here
mirror the migrations; the migrations stay the source of truth for policies, grants and functions.

### `lazy="raise"`
A relationship such as `membership.user` normally loads its related row the first time it is read. In
async code that load cannot run safely, so it fails late or silently. `lazy="raise"` makes the read raise
at once, before any query. We set it on every relationship.

### `deferred`
A column that is left out of a plain `SELECT` until someone asks for it. `app_user` cannot read
`password_hash`, so the model marks it deferred: `select(User)` does not name it, and the database never
sees a forbidden column.

### `set_config(..., true)`
Sets a database setting for one transaction only. The value disappears at COMMIT or ROLLBACK. A pooled
connection is reused by the next request, so a session-level setting would leak; a transaction-level one
cannot.

### `scope="function"`
FastAPI runs the code after a `yield` dependency either before or after the response is sent, depending on
the scope. The commit lives in that code, so the scope decides whether a failed commit can still change the
response.

### `eager_defaults`
When a row is inserted, the database fills in `created_at`. By default SQLAlchemy does not read that value
back, so the attribute stays "expired". Reading it then needs a query, which async code cannot run.
`eager_defaults=True` returns the value with the insert.

### Drift check
A test that compares the ORM models with the migrated database. If someone changes a column in one place and
not the other, the test fails. Alembic's comparison covers tables, columns, keys and indexes. CHECK constraints
need a separate list.

## Problems Encountered & Solutions

### 9. A log line never appeared in the test
- **What happened:** the rowcount test raised correctly, but `caplog` was empty.
- **Why:** the test database fixture runs Alembic. Alembic's `env.py` calls `fileConfig()`, and its default
  turns off every logger that already exists. The rowcount logger existed, so it went silent.
- **Fix:** `fileConfig(..., disable_existing_loggers=False)`. The same fix protects the app when migrations run
  in the same process.

### 10. A failing commit gave a 200 in a quick check
- **What happened:** a deferred foreign key failed at COMMIT, but the route still returned success in the
  default setup.
- **Why:** with the default `scope="request"` FastAPI runs the code after `yield` after the response is sent.
- **Fix:** `scope="function"` on the dependency. A test now checks that a failed commit becomes a 500 and
  stores nothing. A separate check confirmed the default-scope result, so the claim is measured, not assumed.

### 11. Inserted rows had no primary key
- **What happened:** an ORM insert failed with a NOT NULL violation on `id`.
- **Why:** the schema has no default for `id`, because ids are generated in the app (ADR 0005). The models had
  no Python-side default either.
- **Fix:** `default=uuid.uuid4` on each primary key. This adds nothing to the schema, and the drift test still
  passes.

### 12. A rollback test that could not detect the bug it was meant for
- **What happened:** with `set_config(..., false)` (session scope) planted on purpose, the rollback and error
  tests still passed. Only the commit test failed.
- **Why:** PostgreSQL reverts a session setting made inside a transaction that rolls back. Those tests check
  a different failure: a context that survives a rollback.
- **Fix:** none needed in the code. The commit test is the one that catches session scope. The limit is
  recorded in ADR 0011.

## Interview Q&A

**Q: Why does a pooled connection need special care with tenant context?**
A: The pool gives the same physical connection to the next request. A session-level setting would stay on that
connection. A transaction-level setting (`set_config(..., true)`) ends with the transaction, so the next request
starts clean. We prove it by checking the backend process id on one connection.

**Q: What does `lazy="raise"` protect you from?**
A: From a silent extra query in async code, or from a partial result that looks complete. The error appears at
the line that reads the relationship, which is where the fix belongs.

**Q: Why not generate the models from the database?**
A: Alembic's autogenerate misses policies, grants and functions, and we write those as explicit SQL. Generating
the models would also hide drift instead of catching it. The drift test compares two sources that are written
separately.

**Q: A commit fails after your route returned. What does the client see?**
A: With FastAPI's default scope, a 200, because the commit runs after the response. With `scope="function"`, a
500. We use the second, and a test checks it.

**Q: How did you know your leak test would catch the bug?**
A: I planted the bug on purpose (session scope) and checked that the test failed. Two of the four tests did not
catch it, and I wrote down why.

---

# Session 3, Step 3c: login protection in Valkey (2026-10-22)

## Concepts in plain words

### Atomic counter
A count that stays correct when many requests change it at once. A plain read-then-write loses updates when two
requests run together. A Lua script runs on the Valkey server as one step, so the increment and the lock decision
cannot interleave.

### Fixed window
A counter that belongs to one time slot (the 10-minute slot that starts at 12:00, for example). It is simple and
cheap. Its known weakness: a burst can straddle two slots, so it can admit up to twice the limit across a boundary.
The IP limit accepts that trade-off and is recorded in the ADR.

### Keyed hash (HMAC)
A hash that needs a secret to compute. A plain hash of an IPv4 address can be reversed by trying all 4 billion
addresses. A keyed hash cannot, without the key.

### Timing floor
Every login response waits until a fixed time has passed. A known user's failure does a database write that an
unknown email does not, so the response time alone could reveal the account. The floor hides the difference, as long
as the floor is longer than the slowest path. We measured the slowest path and set the floor with margin.

### Fail closed versus fail open
When a dependency is down, a fail-closed system refuses the request, and a fail-open system allows it. Login fails
closed, because it is the path an attacker wants to brute-force. Refresh fails open, because token rotation already
protects it. The choice belongs to each path, not to the whole system.

## Interview Q&A

**Q: Why does a counter need a Lua script rather than two commands?**
A: Two commands (increment, then check) let two requests both read 4 and both decide the fifth failure has not happened
yet. The script does both steps in one server-side call, so the count and the lock decision cannot be split.

**Q: Why not put the hard lock in Postgres, which is durable?**
A: A lock is only as good as its consistency across known and unknown emails. A Postgres row can only be written for a
known user, so an unknown email would never get the same lock. Then the response would reveal which emails exist.
Valkey keyed by HMAC gives both emails the same answer.

**Q: Why is a refused attempt not counted?**
A: If refused attempts counted, an attacker could keep a lock alive forever by continuing to try. The lock expires on
its own schedule, and a refusal never extends it.

**Q: How did you choose the timing floor?**
A: I measured the real paths 40 times each. The slowest was 299 ms. I set 500 ms, which is about 200 ms above it. I
recorded the numbers and the re-measurement rule in ADR 0014, because a faster or slower machine changes the answer.


## Session 4 (Steps 1a to 2a): plain words

### RLS DELETE (and UPDATE) need the row to be visible through SELECT policies
Row-level security (RLS) filters rows with policies. A policy for each command decides which rows that command may touch.
A DELETE or UPDATE does not skip the visibility check: PostgreSQL first has to *see* the row through a SELECT policy.
If the SELECT policy hides the row, the DELETE matches zero rows and raises no error. Nothing fails, and the data stays.

We hit this in Step 2a. The worker could read only `queued` and `processing` bytes, so its DELETE on a `purging` blob removed nothing.
Two tests caught it. The fix widened the worker's SELECT to `purging`, the one status the purge needs. The rule is in ADR 0018 (amendment).

## Session 4 (Step 4 and CI): plain words

### An error-looking log line that is expected
PostgreSQL writes a `FATAL` line for every refused login. Our tests make some logins with wrong passwords on purpose, to prove the refusal works. So the CI log shows four `password authentication failed` lines, and the job still passes.

Before we accepted them, we checked three things. The four lines match the four deliberate tests by role. The count is the same when we run those tests locally. A leaked connection would show up at other times, or with a password that was valid earlier, and neither happened.

We kept the lines and documented them in RUNBOOK ("Expected server log lines in backend-db"). Hiding them would mean lowering the server log level, which would also hide real failures.

**Q: The CI log shows "password authentication failed". Is that a security incident?**
A: Not if the lines match the documented list. A refused login is the server doing its job. The question to ask is whether the count, the roles and the times match what the tests should produce.

**Q: Why not turn off the logging to make the CI log clean?**
A: The log is the evidence that refusals happen. Lowering the level hides real problems too, and one missing line would go unnoticed. A noisy but correct log is safer than a quiet, misleading one.

**Q: How did you tell an expected line from a leaked connection?**
A: I counted the server's `FATAL` lines before and after running only the four tests, locally. The difference was exactly four, with the same roles as CI. I did not guess from the timestamps.

**Q: Why keep tests that fail on purpose?**
A: A login check that never fails proves nothing. The negative tests show that a wrong password is refused, which is the security property. Removing them would make the suite greener and weaker.

**Q: Why did the purge DELETE silently do nothing?**
A: A DELETE only acts on rows it can see through a SELECT policy. The worker's SELECT excluded `purging` rows, so the DELETE found none. Postgres reports "0 rows", not an error. This is why every write path must check its row count (ADR 0011).

**Q: How would you test for this in CI?**
A: Assert the row count, not just the absence of an error, and assert that the data is gone afterwards. The two failing tests did both.

**Q: Why keep `ready` bytes unreadable to the worker?**
A: Least privilege. The worker needs the bytes only while extracting text (`queued`, `processing`) and while purging (`purging`). Widening the read to `ready` would give a compromised worker every live file, so any such change must be a new migration with a reason.

## Session 4 (guard incident and ops): plain words

### A safety check that compares against an optional setting passes silently when the setting is missing
Our test guard refused a test run from touching the dev database only if it could compare the admin URL with a dev URL from the environment. Our `.env` had no dev URL, so the comparison had nothing to compare against, and every check passed. A test run then reached the dev cluster and reset the dev login roles' passwords. The dev database holds the same kind of seed data as the tests, so we saw the test tenants there (`Acme`, `Globex`).

The fix has two rules. First, refuse the dangerous case unconditionally: the dev port is refused on its own, whatever is configured. Second, check every path that reaches a server, not only the one we look at. The admin connection and the psql command must reach the same cluster, and we compare their `system_identifier` from `pg_control_system()` before anything is changed.

**Q: Why not compare with the dev URLs, and refuse only when they match?**
A: A check that depends on an optional setting is only as strong as that setting. When the setting is missing, the check says "nothing to compare, so pass". Refuse the dangerous case directly, and keep the comparison as a second check.

**Q: What does "verify every path reaches the same server" mean here?**
A: The test fixture reaches the database in two ways: a connection through the admin URL, and the psql command, which runs in a container. If the two reach different clusters, a bootstrap that sets passwords can change the wrong server. Compare an identity that each path reports, here the cluster's system identifier, and refuse on a mismatch.

**Q: How did you know the fix works?**
A: With the admin URL on the dev port, the guard refused before any change, and the dev data was the same afterwards. With the test cluster as the admin URL and the psql command pointed at the dev container, it refused with a message that names the mismatch, again before any change. Each refusal has a unit test as well.

## Session 4 (worker startup login check): plain words

### A failed login has no SQLSTATE when the connection itself fails
A SQLSTATE is a five-character error code that PostgreSQL sends with an error (for example `28P01` for a wrong password). We expected the driver to expose it on a failed login, but psycopg raises the connection error without one, so the code was `None`. The server's wording, `password authentication failed`, was the only reliable signal. When we probed the real driver, we saw the same wording in every wrong-password attempt.

**Q: Why not just check the SQLSTATE?**
A: Check what the driver actually returns, with a probe, before you write the rule. Our first version looked right on paper, and our tests caught that it never matched. A rule that never matches fails safe here, because the worker still exits non-zero, but it gives the wrong message.

**Q: Why does the worker exit instead of retrying a wrong password?**
A: A wrong password does not get better with time. Retrying it forever is the loop we saw in the smoke test. A bounded reconnect is a different case (a database that restarts), and it needs a supervisor to restart the worker if it gives up. We deferred it to the deployment session.
## Session 5, Step 1 (retrieval evaluation harness): plain words

### Equal scores need a tie-break that does not depend on random ids
Keyword search ranks with `ts_rank_cd`, which gives coarse values (2.7, 1.7, 1.6). Many chunks share a score. We broke ties by `version_id`, a random UUID created at each upload. Two loads of the same files ranked differently, and recall moved by a pair or two (hybrid 0.889, then 0.867). A harness is only trustworthy if the same input gives the same output, so we compared two runs and saw they differed. The fix is a tie-break from the text itself (the md5 of the chunk body). After that, two runs matched.

**Q: Why does a tie-break matter for a search system?**
A: Without a rule, the order among equal scores is whatever the database returns, and that can change between runs. A recall number that changes with the order is not a measurement. It also makes bugs hard to reproduce.

**Q: How did you find it?**
A: I ran the full evaluation twice and diffed the JSON reports. Vector was identical, keyword and hybrid were not, and every difference was in tied scores.

### Run the harness as the role that production uses
The rankers run as `app_user` inside a tenant transaction, so row-level security and the current-version rule apply. The harness does not filter by tenant or version itself, so a retired document is invisible because the database says so. To prove the harness would notice a leak, one test runs the same questions as the admin role, which bypasses row-level security, and the retired-chunk counter must go above zero. A control that cannot fail proves nothing.

**Q: Why not filter by version in the query?**
A: Then the query would be testing itself. Using the production role tests the policy, which is the control we rely on.

### A surprisingly good number needs a second look
Hybrid retrieval scored 0.889 where our earlier notes said 0.76. A jump like that is a reason to check, not to celebrate. The check found the tie-break problem, and the final figure (0.867) still differs from the old one. We could not explain that difference because the old script is gone, so the report says so and treats it as a hypothesis for the next step.

**Q: What do you do with a metric that is much better than before?**
A: Recompute it from the raw per-question data, run it twice, and look for what changed in the method before believing it.

## Session 5, Step 2 (retrieval strategy): plain words

### A prepared statement keeps its plan, so a planner setting changed later is ignored
We wanted to compare how PostgreSQL searches with different settings (`enable_seqscan`, `enable_indexscan`, and pgvector's `hnsw.ef_search`). The first run gave the same answer for every setting, and some "forced index" runs behaved like full scans. The cause: the database driver (psycopg) turns a query into a prepared statement after it has run a few times. A prepared statement keeps its plan, and the planner switches are read only when the plan is made. Every setting used the same SQL text, so after a few runs they all shared one cached plan. The `EXPLAIN` statements, which have different text, were planned fresh, and that is why they showed a different plan from the one that actually ran.

The fix is a unique SQL text per setting (a comment with the label in front of the query). The results of the first run were thrown away. Settings that are read while the query runs, like `hnsw.ef_search`, are not affected; settings that steer the planner are.

**Q: How did you notice?**
A: The numbers were too regular. Every setting returned exactly 50 rows with identical neighbours, including a setting that, as I knew, can return at most 40. And the plan shown by `EXPLAIN` disagreed with the speed of the query. When a result contradicts what you know the system can do, suspect the measurement first.

**Q: Does this matter in production?**
A: Yes, in one way. If the app ever sets a planner option per request, a prepared plan would ignore it. The vector search sets nothing like that: it relies on the planner's own choice.

### An approximate index that filters after its scan can quietly return the wrong rows
HNSW finds the nearest rows in the whole table, then row-level security removes the ones the tenant may not see. With many tenants, most of the nearest rows belong to someone else, so a tenant may get fewer rows than asked for, or not the true nearest. The planner picked the index for a tenant once it had about 2,000 chunks, and from then on the top 5 matched the exact top 5 only about 30 percent of the time (29 to 34 percent in the committed test). No error was raised. Tuning (a larger `ef_search`, iterative scans) helped on some data and failed on near-duplicate content, which policy documents have (versions, boilerplate).

The decision was to drop the index and search the tenant's rows exactly. The tenant-first primary key narrows the search to that tenant, so the cost depends on the tenant's size, not the table's.

**Q: Why not keep the index and tune it?**
A: Correctness would depend on the planner and on how similar other tenants' documents are. Both change without notice. Exact search is slower at 10,000 chunks and more, but it is always right, and we wrote down when to revisit it.

**Q: What would you do at 100,000 chunks for one tenant?**
A: Partition the chunks by tenant (or build an index per tenant), so each index holds only one tenant's rows and there is nothing to filter after the scan.

## Session 5, Step 3b (answer providers): plain words

### "Strict" JSON output from a model is a request, not a guarantee
We asked Groq for strict JSON-schema output, and one call in about 61 still came back with JSON that broke the schema. Groq did not return it. It answered HTTP 400 with the code `json_validate_failed`. My first version treated every 400 as "your request is wrong" and stopped the run, which would also have hidden the problem. Now a 400 with that code is a different error ("the model's output was invalid"), and any other 400 stays "bad request". The two need different reactions: a bad request fails every time, while a bad generation may pass on the next call.

**Q: Why not trust strict mode?**
A: The model generates text and a validator checks it afterwards. When they disagree, the provider reports an error. The application must expect that path and must still validate what it reads (citation numbers, types) itself.

**Q: How did you find it?**
A: By running the real provider on all the questions, not only the fake HTTP tests. The fake responses only contain what we imagined.

### Keep keys out of URLs
Google's reference examples put the key in the URL (`?key=...`). URLs end up in logs and error messages. We send it in the `x-goog-api-key` header instead, and a test checks that the key is in no URL, body, log line, `repr` or error message. The HTTP library's own logger can print headers at DEBUG, so it is held at WARNING.

### A model that worked yesterday may be closed to new accounts
`gemini-2.5-flash-lite` answered 404 ("no longer available to new users") for a new account, though Google's docs list it as not deprecated. Vendor model lists describe the vendor, not your account. The runner stops on the first rejected call, so a closed model costs one request, not a whole day's quota.

### Free-tier limits are a budget to plan
Groq's free tier allows 8,000 tokens a minute and 200,000 a day. A 50-question run of 1,570-token prompts is about 78,000 tokens and 13 minutes. The runner paces its calls, stops at a token budget, and the report says what was used.

### `httpx2` sat in the scaffold unnoticed: review every direct dependency by exact name, not by resemblance
The first scaffold commit declared `httpx2` as a dev dependency, probably meaning `httpx` (used by the web framework's test client). Our code never imported it (Starlette's test client picked it up when present), so nothing broke, and it stayed through every session. It had never been reviewed by exact name: an unverified package with a name close to a well-known one, and my own live runs sent API keys through it before the review caught it. The review note was right to ask for the exact name, and it was replaced by the established library that was already installed. Later evidence (checked 2026-10-26) shows the package is probably not hostile: Starlette 1.9.0's own test client deprecates the `httpx` fallback in favour of `httpx2`, and the repository is `pydantic/httpx2`, from the httpx maintainers, so it is likely httpx's successor (a watch item in `docs/DEPENDENCIES.md`). The Session 1 notes in this log also record that it was added on purpose, for Starlette's test client, so the real gap was the missing review by exact name, not an unknown package.

What changed: the real `httpx` is now the declared dependency, `docs/DEPENDENCIES.md` lists every direct dependency with its exact PyPI name, licence, link and reason, and a unit test fails when `pyproject.toml` and that list disagree (checked by adding a fake `httpx2` and watching it fail).

**Q: How would you stop a typosquatted dependency?**
A: Pin and review direct dependencies by exact name and owner, keep a short allow-list that CI compares with the project file, read the lockfile diff on every dependency change, and never put real secrets through a package nobody has looked at. Hash-pinned lockfiles stop a package changing after review but not a wrong package chosen at review time.

**Q: What was the cost of the mistake?**
A: Two API keys went through an unreviewed package, so they are rotated as a precaution.

## Session 5, Step 4 (the answer contract): plain words

### Check what a model says against what it was given, in code
A model can return a citation that looks perfect and is wrong: a source number that was never given, a quote that is not in the source, or a number that appears nowhere in the evidence. So the answer is not shown until our code has checked three things: the source number is one of the k sources, the quote really appears in that source (after the same cleaning of whitespace, quotes, dashes and case on both sides), and every number in the answer appears in a quote. If any check fails the answer is thrown away, the model gets one more try with a fixed reminder, and if it fails again the user gets our own refusal text. The user never sees broken output.

**Q: Does a valid quote mean the answer is right?**
A: No. It means the quote exists. The answer could still say more than the quote supports. That is measured separately (groundedness, Step 7). The number check catches one common drift and nothing more.

**Q: Why is one bad citation enough to throw the answer away?**
A: The answer does not say which sentence a citation supports. If we dropped the bad one, a claim could stay on screen with nothing behind it. The cost is some false refusals, so we count them and tune later with data.

### A refusal and an error are different things
If the documents do not contain the answer, that is a refusal: a normal result with our own wording. If the provider times out or rejects the key, that is an error: the service is unavailable. Mixing them hides outages inside the refusal rate and tells the user the wrong thing.

### Real data beat the plan
The plan said to find the clause by looking for numbers at the start of lines. On the real chunks that found only 28 of 45 expected clauses, because extraction joins sub-clauses into one line. Looking at the actual text first led to a position-based rule that finds all 45. A planted-defect run also caught one gap in my own tests (a label excluded for two reasons, so removing one reason changed nothing).

### Keep model words away from users on a refusal
A refusal carries a fixed reason code, and the wording comes from our code. A model that was tricked by text inside a document cannot put its own words in front of an employee that way.

## Session 5, Step 5a (metering and tenant settings): plain words

### Make the database refuse what the code must never store
We want a usage table that can never hold what an employee asked. A rule like "the code does not write the question there" depends on every future change remembering it. So the table has no column that can hold a sentence: the few text columns accept only values from a fixed list, enforced by CHECK constraints in the database. A test tries to write a sentence into each text column and expects a refusal. If someone adds a new text column, another test fails until a person decides it may exist.

**Q: Why put the allowed list in the database and not only in code?**
A: A bug in code can store anything. The database is the last gate: it rejects a value that is not on the list whoever sends it. The cost is that the list exists twice (migration and code), so a test compares them.

### A setting that can only be changed by the operator is part of the security model
Which AI provider may see a tenant's documents is a privacy decision, not a preference. So the web app can only read the setting. Only the operator's tool can change it, it checks the combination (real data with a free-tier provider is refused), and it writes an audit row with the old and new value. The same check runs again before every call, so a wrong value in the database still cannot send real data to a demo-only provider.

**Q: Why check the provider policy twice?**
A: Once when the setting is written, so a mistake is caught early and explained. Once before each call, because the database could be edited by another route, and the cost of a wrong call (client data sent to a provider that may train on it) cannot be undone.

### Money as whole numbers
Costs are stored as millionths of a dollar in an integer column, and the CLI reads dollars with `Decimal`, not `float`. Floats cannot represent 0.05 exactly; sums of them drift. A cap compared against a drifting sum would sometimes allow one request too many or too few.

### A name can be too long for the bookkeeping table
The first migration id was 37 characters, and Alembic's version table holds 32. Every database test failed at setup with "value too long". The fix was a shorter id before anything persistent had used it. Once a migration is applied somewhere persistent, its id can never be changed, so check the length when you name it.

## Session 5, Step 5b (query embedding in the web process): plain words

### Choose a limit from a measurement, and measure the right thing
We needed a number: how many questions may be embedded at once. A guess would be 4 (the CPU count). The measurement said memory does not change with the number at all (the model weights are shared), and with as many callers as slots the speed gain stops at 2: 1 slot about 48 per second, 2 slots about 64, 4 slots about 67, which is inside the difference between two runs. With four callers at once, 4 slots finished sooner (median 52 ms against 67 ms) but stalled the event loop longer (62 ms against 24 ms). Neither difference is something a user would notice, so the default is 2, with the numbers in the evaluation file and a command to measure again on a new machine. One result is still unexplained: 2 slots with four callers was slower per second than 2 slots with two callers, in both runs. It is written down as unexplained, not guessed at.

**Q: Why run each case in its own process?**
A: Memory numbers include everything the process did before. A fresh process per case means the figure belongs to that case only.

**Q: Why run the whole matrix twice?**
A: To see how much a number moves by chance. Here about 8 %, so a 4 % gain is not a gain.

### A thread cannot be cancelled, so the slot belongs to the thread
When a request gives up after 3 seconds, the work keeps running in its thread. If the code freed the slot at that moment, the next request could start another thread, and a slow period would pile up more and more running work. So the slot is released when the thread finishes, and a test proves that a timed-out request does not let a second thread start.

**Q: What does `/ready` returning 503 while the model loads buy you?**
A: A load balancer or a person can tell "alive but not ready" from "dead". The app answers `/health` at once, takes no questions it cannot answer, and a failed load stays visible instead of being a log line nobody reads.

### A test that cannot fail points at redundant code
One planted defect (skipping a state check) was not caught. The reason: the model is only set when the load succeeded, so the state check could never decide anything the model check did not. The fix was to delete the redundant check, not to write a test for unreachable behaviour.

## Session 5, Step 5c-1 (the `/ask` flow): plain words

### Put the order of the checks in a test
The flow runs the cheap checks first (Valkey), then the database, then CPU, and the paid model call last, so a request that will be refused costs almost nothing. A test records every stage in one list and asserts the exact order, and asserts that a refused request never reached the later stages. Without that, someone can move the embedding in front of the cap check and nothing fails.

**Q: What does "fail closed" mean in this flow?**
A: When a safety check cannot run (Valkey down, the database slow, no settings row), the answer is "no", not "probably fine". Every check before the model call has a test where the check itself breaks and the model is still never called.

### A slot that cannot leak
Limiting how many questions run at once needs a counter. A plain counter that goes up and down leaks if a process is killed between the two. Giving the counter an expiry does not fix it, because other requests keep refreshing the expiry. So each running request is its own entry with its own expiry time in a sorted set. A killed request's entry simply times out, and nothing else can keep it alive. A test plays exactly that: two requests that never finish, healthy traffic around them, and the slots coming back after the hold time.

### If the bookkeeping fails, stop serving
The daily cap is a sum of rows. If writing the rows fails silently, the sum stays at zero and the cap never trips. So a failed write is tried twice, and if both fail, a flag blocks that tenant for five minutes. The user who was already answered still gets their answer, because they did nothing wrong and the model was already paid for. The flag turns a silent failure into a visible one.

### Log a hash, not an id
Logs are copied around, searched and kept. A tenant id in a log is a tenant id everywhere that log goes. A keyed hash of the id lets an operator follow one tenant's requests without exposing the id, and it cannot be reversed or guessed without the key. A test reads the source of every log call in the project and fails if one names a tenant, user, document or email value; a second test puts known strings into a request and searches every log record for them.

**Q: Why two tests for the same rule?**
A: The first reads code and is cheap, but it can only see names. The second runs the real flow and sees values. Each catches what the other cannot.

### Read your own claim twice
While writing up the embedding measurements I first claimed the mean latency was the same for every limit. Re-reading the table showed it was not (the throughput differed). The wording was fixed before anyone relied on it. Numbers in a table are easy to summarise wrongly.

## Session 5, Step 5c-2 (`POST /ask` over HTTP): plain words

### A lighter check must be proven not to be a weaker check
The endpoint takes its caller from a new dependency that gives its database connection back before the slow work. That is only safe if it refuses everything the old dependency refuses. So one test sends the same bad requests (no header, expired token, wrong key, a suspended tenant, a changed role, and more) to both and requires the same status, body and header. The two dependencies also share their code, so they cannot drift quietly, and a contrast test shows the old one really does hold a connection, so the "no connection held" test is able to fail.

**Q: How do you test that a connection is not held during a slow call?**
A: Ask the database. While the fake model sleeps, a probe reads `pg_stat_activity` and counts transactions of the app login that are "idle in transaction". It must be zero. A second route using the old dependency must show one, or the probe proves nothing.

### Test fixtures can reach real services
The first run of the HTTP tests tried to make one real request to Gemini (it ended as a timeout). One test named Gemini as the tenant's provider, and the app builds its adapters from the keys in `backend/.env`, which are real. The text was fictional and the quota cost nothing, but the lesson is general: when a test starts the real application, it also starts everything the application builds from real configuration. The fix has layers: the fixture swaps the provider registry for one that holds only a fake and asserts that; every test now runs with the provider keys removed and Settings not reading `backend/.env`; and a network guard refuses any connection that leaves the machine and fails the test at its end, so even an adapter that turns the refusal into "provider unavailable" cannot hide the attempt. A throwaway test that builds a real adapter fails on the guard, and each weakened version of the guard is caught. A rule that is only remembered is a rule that gets broken.

### Test the wiring, not only the parts
The limits are tested with a limiter built in the test. That would pass even if the application built its limiter with the two numbers swapped. A separate test reads the running application's parts and checks them against the settings, and a second app started with other settings must be built with those.

### Measure what happened before saying it
In the first draft of the dependency note I wrote that a package was a near-miss of another. Checking again, for an unrelated reason, showed that the framework itself recommends it and that its maintainers are the same people. The earlier caution was reasonable at the time, but the wording was too strong. The fix is to say what was checked and when, and to correct the record when new evidence arrives.

### Tests that commit rows must remove them, and run them in the same order CI does
CI failed on one old test after the `/ask` tests were added. That test runs as the database administrator, which sees every tenant's rows, and it expects a database with no one else's chunks. My new tests had committed chunks for their own tenants and left them behind. On my machine the suite ran in three chunks, each on a fresh database, so the leftovers never met the test; CI runs everything in one go, and they did. The fix is a shared cleanup used by every test that commits tenant data. The lesson is two rules: a test that commits shared data cleans up after itself, and before pushing, run the suite the way CI runs it (one invocation), not in pieces that hide contamination.

**Q: Why did the chunked local runs pass when CI failed?**
A: Each chunk rebuilt the database, so data left by one test could not reach a test in another chunk. The failure needed the leftover data and the sensitive test in the same run.

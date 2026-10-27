# Runbook — Development, Deployment, and Debugging

## Local Development

### Prerequisites
- Python 3.12+ with uv
- Node.js 24+
- Docker and Docker Compose v2
- PostgreSQL and Redis services stopped (or run via docker-compose)

### Setup

```bash
# Backend
cd backend
uv sync --locked
cp .env.example .env
# Edit .env if needed (default: connect to docker-compose services)

# Frontend
cd frontend
npm install
cp .env.example .env.local
# Edit .env.local if needed (default: http://127.0.0.1:8000)
```

### Dev Host Rule (ADR 0004)

Use **`127.0.0.1` everywhere in development**, not `localhost`:

| Setting | Value |
|---|---|
| Frontend URL | `http://127.0.0.1:3000` (set by `npm run dev`, which runs `next dev -H 127.0.0.1`) |
| API URL | `http://127.0.0.1:8000` (`NEXT_PUBLIC_API_URL` in `frontend/.env.local`) |
| CORS | `CORS_ORIGINS=http://127.0.0.1:3000` in `backend/.env` |

**Why:** the browser treats `localhost` and `127.0.0.1` as different hosts. A cookie set by one is not sent to the other, so login refresh would fail silently. Node also resolves `localhost` to `::1` first, while uvicorn binds `127.0.0.1`.

If you already have a `backend/.env` with `CORS_ORIGINS` containing `localhost`, change it to the value above. Each developer updates their own local `.env` and `.env.local`, since they are git-ignored.

### Running Services

```bash
# Start PostgreSQL and Valkey via Docker Compose
docker compose up -d

# Verify services are healthy
docker compose ps

# Backend (uses --factory to load app from create_app function, not module-level import)
# --loop: psycopg async needs a selector loop. On Windows uvicorn defaults to ProactorEventLoop,
# which psycopg does not support (ADR 0005). Linux/macOS are unaffected but the flag is harmless.
cd backend
uv run uvicorn grounded_answers.main:create_app --factory --loop grounded_answers.runtime:selector_loop_factory --reload

# Frontend (in another terminal)
cd frontend
npm run dev
```

Visit http://127.0.0.1:3000 (frontend) and http://127.0.0.1:8000/health (backend). See Dev Host Rule above.

### Health and readiness (ADR 0009)

| Endpoint | Question it answers | Checks | 200 | Not OK |
|---|---|---|---|---|
| `GET /health` | Is the process running? | None | `{"status":"ok"}` | No response (process down) |
| `GET /ready` | Can it serve requests? | PostgreSQL as `app_user` (`SELECT 1`); Valkey `PING`; schema head equals `EXPECTED_SCHEMA_HEAD` | `{"status":"ready"}` | `503 {"status":"not_ready","failed":[...]}` |

- `failed` holds only `"database"`, `"cache"` or `"schema"`. Hostnames, messages and versions are never in the response.
- If the database fails, `schema` is not listed. It fails for the same reason, so only the root cause is named.
- Each check has a 1 s timeout. All checks together have a 2 s budget. Results are cached for 2 s, so a load balancer polling `/ready` never opens a connection per request.
- The server log holds the detail, one line per failed check, for example `readiness check failed: schema (ProgrammingError)` or `readiness check failed: schema (version mismatch: found 0003_policies_and_definers, expected 0004_alembic_version_grant)`. The log never holds an exception message, because messages can contain hosts or credentials.

Check it:

```powershell
curl.exe -i http://127.0.0.1:8000/ready
```

## Troubleshooting

### Port 5432 (PostgreSQL) Already in Use

**Symptom:** `docker compose up` fails with "bind: address already in use".

**Cause:** Local PostgreSQL service is running on port 5432.

**Fix:**

**Option 1: Stop local PostgreSQL service**
```powershell
# Windows: Check what's using port 5432
netstat -ano | findstr :5432

# Stop the service (e.g., PostgreSQL from pgAdmin or installer)
# Or via Services.msc: locate PostgreSQL and stop it
```

**Option 2: Change the host port in docker-compose.yml**
```yaml
# docker-compose.yml
postgres:
  ports:
    - "127.0.0.1:5433:5432"  # Map 5433 on host to 5432 in container
```

Then update `backend/.env`:
```
DATABASE_URL=postgresql+psycopg://postgres:change-me-local-only@127.0.0.1:5433/grounded_answers
```

Restart docker-compose:
```bash
docker compose down
docker compose up -d
```

### Dev bootstrap problems (Session 2)

**App crashes at startup: `migration_database_url: Extra inputs are not permitted`**
- Cause: `MIGRATION_DATABASE_URL` is in `backend\.env`. The web app loads that file, and `Settings` forbids unknown keys.
- Fix: delete that line. Set the migration URL in the shell only (see "Migration credentials"). `tests/test_env_example.py` guards the example file.

**A DB test run stops `app_user` or `migration_role` from logging in**
- Cause: the test admin URL pointed at the dev cluster. The fixture resets the three login roles' passwords on every run.
- Fix: rerun dev bootstrap steps 3 and 4a with the passwords you intend to keep, then `verify_login.py`. Keep `TEST_ADMIN_DATABASE_URL` on port 5433. The guard refuses the dev host and port, but only when `backend\.env` has a `DATABASE_URL`.

**`ROLE_SCRAM is not set` (or another SCRAM variable) when a reset or bootstrap runs**
- Cause: `docker compose exec -e NAME` forwards only the variables it names. The variable is set in PowerShell, but it never reaches the container.
- Fix: name it in the command: `-e ROLE_SCRAM`. The same rule applies to `PSQL_COMMAND` for the test cluster.

**`no such service: postgres-test`**
- Cause: the test cluster is in a compose profile, so plain `docker compose` ignores it.
- Fix: add `--profile test` to every command that touches `postgres-test`.

**`/ready` returns 503 `schema`, and the log says `ProgrammingError`**
- Cause: the table `grounded.alembic_version` does not exist. Migrations have not run on this database.
- Fix: run dev bootstrap step 7 (`alembic upgrade head`).

**`/ready` returns 503 `schema`, and the log says `version mismatch`**
- Cause: the database is at an older or newer head than the code expects.
- Fix: run `alembic upgrade head` (older) or deploy matching code (newer). Check with `alembic current`.

**`password authentication failed` (SQLSTATE `28P01`) from `verify_login.py`**
- Cause: the password you typed does not match the one stored in the cluster.
- Fix: "Reset one role's password" for that role. Update `backend\.env` if it is `app_user`.

**Connection string errors after a password change**
- Cause: a password character such as `@`, `:`, `/` or `#` breaks the URL.
- Fix: use only letters, digits, `-` and `_`. Regenerate with `secrets.token_urlsafe(24)`.

**`psycopg async needs a selector event loop` on Windows**
- Cause: uvicorn started without the selector loop flag, so it uses the Proactor loop.
- Fix: start the API with `--loop grounded_answers.runtime:selector_loop_factory` (see "Running Services").

### Gitleaks Fails on First Push

**Symptom:** GitHub Actions security job fails with:
```
fatal: ambiguous argument '<sha>^..<sha>': unknown revision or path not in the working tree
```

**Cause:** On the first push to a repo, the root commit has no parent. Gitleaks tries to scan `parent..commit` but parent doesn't exist.

**Fix:**
- This is expected and harmless on initial push. Gitleaks will scan normally on subsequent commits (which all have parents).
- No action required; the failure is transient.

### Run Local Gitleaks Scan

**Full scan (v8.24.3):**
```powershell
# Requires Docker
docker run --rm -v "${PWD}:/repo" zricethezav/gitleaks:v8.24.3 detect --source /repo -v
```

**Expected output (no leaks found):**
```
No leaks detected.
```

**If false positives appear:**
- Check `.gitleaks.toml` allowlist in the repo root
- Verify the regex matches your case (e.g., Docker image tags like `7.4.11-alpine3.24`)
- Run with `--config .gitleaks.toml` to load custom rules

## CI/CD

CI runs on every push to `main` and on pull requests. See `.github/workflows/ci.yml` for job definitions.

### Running Locally

```bash
# Backend
cd backend
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/ tests/
uv run --with pip-audit pip-audit
uv run pytest tests/

# Frontend
cd frontend
npm ci
npx eslint src/ --max-warnings 0
npx tsc --noEmit
npm audit --audit-level=high
npm run build
```

### Expected server log lines in backend-db

The `backend-db` job's "Stop containers" step prints the PostgreSQL server log. It must show exactly these six lines, each `FATAL: password authentication failed for user "..."`. The count was four until the worker startup login check (branch `feat/worker-login-check`) added two `worker_user` attempts:

| Role | Test that causes it |
|---|---|
| migration_role | `tests/db/test_login.py::test_wrong_password_is_refused` |
| app_user | `tests/db/test_login.py::test_wrong_password_is_refused` |
| worker_user | `tests/db/test_login.py::test_wrong_password_is_refused` |
| app_user | `tests/db/test_role_scripts.py::test_login_check_reports_a_wrong_password` |
| worker_user | `tests/db/test_worker_login_check.py::test_a_wrong_worker_password_is_refused_with_no_url_or_password` |
| worker_user | `tests/db/test_worker_login_check.py::test_main_stops_the_worker_on_a_wrong_password` |

They are deliberate. Each one is a login with a wrong password, which the tests use to prove that PostgreSQL refuses it. PostgreSQL always logs a refused login, and the job still passes. Do not hide these lines by lowering the server log level: that would also hide real failures.

A different count, a different role, or a line at another time means something changed. Investigate before merging. To check locally (set `TEST_ADMIN_DATABASE_URL` and `PSQL_COMMAND` first, as in "Running Database Tests" below):

```powershell
# Count the FATAL lines, run the six tests, count again. The difference must be 6.
$before = (docker compose --profile test logs postgres-test 2>&1 | Select-String 'FATAL' | Measure-Object).Count
cd backend
uv run pytest -m db -k "wrong_password or wrong_worker_password"
cd ..
$after = (docker compose --profile test logs postgres-test 2>&1 | Select-String 'FATAL' | Measure-Object).Count
"new FATAL lines: $($after - $before)"
```

`-k wrong_password` also selects `test_auth_flows.py::test_wrong_password_is_generic_and_audited`. Measured: it adds no server line, so the count is still 6.

## Dependency Upgrades (ADR 0005)

- **Wait 7 days after a release before a manual upgrade.** Dependabot applies the same cooldown to automatic PRs. Security advisories are not delayed.
- Do not add a date-based hold (`exclude-newer`) to `pyproject.toml`. A forgotten hold would also block security fixes.
- The lock file keeps existing pins. Only `uv lock --upgrade` or `--upgrade-package` moves them.

## Running Database Tests (ADR 0006)

DB tests run on their own PostgreSQL cluster, `postgres-test`, on port **5433**. The dev cluster is on 5432. Each test run resets the passwords of `migration_role`, `app_user` and `worker_user` on the cluster it targets. So the test admin URL must never point at the dev cluster. The guard (`tests/db/guard.py`) refuses an admin URL on the same host and port as a dev URL (dev URLs come from `backend\.env`).

DB tests fail, never skip, when the test cluster is unavailable. Start it only when you run DB tests:

```powershell
cd C:\Users\Admin\Desktop\portfolio-v2\grounded-answers
docker compose --profile test up -d postgres-test redis
docker compose --profile test ps postgres-test
```

Expected: `postgres-test` shows `Up ... (healthy)`. Wait until it is healthy. Redis (Valkey) is needed for the readiness DB tests.

```powershell
cd C:\Users\Admin\Desktop\portfolio-v2\grounded-answers\backend

# Admin URL for the TEST cluster (port 5433, maintenance database 'postgres').
# Built from the root .env without printing the password:
$t = @{}
Get-Content ..\.env | ForEach-Object { if ($_ -match '^(TEST_POSTGRES_USER|TEST_POSTGRES_PASSWORD)=(.*)$') { $t[$matches[1]] = $matches[2] } }
$env:TEST_ADMIN_DATABASE_URL = "postgresql://$($t.TEST_POSTGRES_USER):$($t.TEST_POSTGRES_PASSWORD)@127.0.0.1:5433/postgres"

# psql runs inside the test cluster's container (same PostgreSQL 17 image as the server).
$env:PSQL_COMMAND = 'docker compose --profile test exec -T -e MIGRATION_ROLE_SCRAM -e APP_USER_SCRAM -e WORKER_USER_SCRAM -e ROLE_SCRAM postgres-test psql'

uv run pytest -m db -v          # database tests
uv run pytest -m "not db"       # unit tests (no database needed)
```

The fixture drops and recreates `grounded_answers_test` on the test cluster only. The guard refuses any other target before it connects.

### Dev database bootstrap (first time, or after resetting the dev cluster)

Same SQL the tests run (ADR 0006). Passwords are typed at a hidden prompt and never appear on the command line. Run from the repo root.

**1. Start the dev cluster and Valkey.** `postgres` must show `(healthy)`.

```powershell
docker compose up -d postgres redis
docker compose ps
```

**2. Set the bootstrap admin URL** (dev superuser, from the root `.env`, password not printed):

```powershell
$d = @{}
Get-Content .env | ForEach-Object { if ($_ -match '^POSTGRES_PASSWORD=(.*)$') { $d.pw = $matches[1] } }
$env:BOOTSTRAP_ADMIN_URL = "postgresql://postgres:$($d.pw)@127.0.0.1:5432/postgres"
```

**3. Set the role passwords.** Type each password twice at the hidden prompt. Keep them: `app_user` and `migration_role` go into `backend\.env` (step 7).

```powershell
$env:APP_USER_SCRAM       = uv run --directory backend python scripts/make_scram_verifier.py app_user
$env:WORKER_USER_SCRAM    = uv run --directory backend python scripts/make_scram_verifier.py worker_user
$env:MIGRATION_ROLE_SCRAM = uv run --directory backend python scripts/make_scram_verifier.py migration_role
```

**4a. Login roles** (roles and passwords). Needs the verifiers from step 3. Expect `ALTER ROLE` lines, no `ERROR`. Safe to re-run.

```powershell
Get-Content -Raw backend\scripts\bootstrap_cluster.sql | docker compose exec -T -e MIGRATION_ROLE_SCRAM -e APP_USER_SCRAM -e WORKER_USER_SCRAM postgres psql -U postgres -d postgres -v ON_ERROR_STOP=1 -f -
if ($LASTEXITCODE -ne 0) { throw "login role bootstrap failed" }
```

**4b. Definer roles** (no secrets; a definer role never needs the verifiers). Expect `GRANT` lines, no `ERROR`. Safe to re-run. To add a definer role later, add its name to `backend\scripts\bootstrap_definer_roles.sql` and run this same command.

```powershell
Get-Content -Raw backend\scripts\bootstrap_definer_roles.sql | docker compose exec -T postgres psql -U postgres -d postgres -v ON_ERROR_STOP=1 -f -
if ($LASTEXITCODE -ne 0) { throw "definer role bootstrap failed" }
```

**5. Database bootstrap** (CONNECT grants, schema). Expect `GRANT` and `CREATE` lines, no `ERROR`.

```powershell
Get-Content -Raw backend\scripts\bootstrap_database.sql | docker compose exec -T postgres psql -U postgres -d grounded_answers -v ON_ERROR_STOP=1 -f -
if ($LASTEXITCODE -ne 0) { throw "database bootstrap failed" }
```

**Session 4, Step 1a (ADR 0016):** this bootstrap also creates the `queue` schema and gives `app_user` and `worker_user` the search path `grounded, queue`. On a database that was bootstrapped before this step, run step 5 again **before** `alembic upgrade head` (migration `0005`). Migration `0005` fails with "schema queue is missing" otherwise. The bootstrap is idempotent.

**Session 5, Step 5a (ADR 0021): pgvector.** On a dev database bootstrapped before this step, run step 5 again **before** `alembic upgrade head`. It creates the `extensions` schema, grants `USAGE` on it to the three login roles only, installs the `vector` extension there, and adds `extensions` to each role's search path. Migration `0008_chunks` needs the extension and fails without it. The role search path applies to **new** connections, so restart the web app and the worker after this step. The bootstrap is idempotent.

Check the extension (expect one row, schema `extensions`):

```powershell
docker compose exec -T postgres psql -U postgres -d grounded_answers -c "SELECT e.extname, n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector';"
```

**6. Verify the three logins** (see "Verify login" below). Expect three `LOGIN OK` lines.

```powershell
uv run --directory backend python scripts/verify_login.py app_user worker_user migration_role
```

**7. Migrations to head.** First define `Set-MigrationUrl` in this shell (code in "Migration credentials" below). Expect the last migration line to be `Running upgrade ... -> 0004_alembic_version_grant`, exit code 0.

```powershell
Set-MigrationUrl
uv run --directory backend alembic upgrade head
Remove-Item Env:MIGRATION_DATABASE_URL
docker compose exec -T postgres psql -U postgres -d grounded_answers -tAc "SELECT version_num FROM grounded.alembic_version"
```

The last command prints `0004_alembic_version_grant`.

**8. Point `backend\.env` at the app role.** Use the `app_user` password from step 3. The file must not contain any migration credential.

```
DATABASE_URL=postgresql+psycopg://app_user:<app_user password>@127.0.0.1:5432/grounded_answers
```

Passwords must use only URL-safe characters (letters, digits, `-` and `_`). A character such as `@` or `:` breaks the URL.

**9. Start the API and check readiness.** Terminal 1 runs the API; terminal 2 checks it.

```powershell
# terminal 1
uv run --directory backend uvicorn grounded_answers.main:create_app --factory --loop grounded_answers.runtime:selector_loop_factory --port 8000

# terminal 2
curl.exe -i http://127.0.0.1:8000/ready
```

Expect `HTTP/1.1 200 OK` and `{"status":"ready"}`. On 503, the body names the failed dependency: `database` (check `DATABASE_URL` and the cluster), `cache` (Valkey is not running), or `schema` (step 7 did not finish).

`BOOTSTRAP_ADMIN_URL` is the **dev** superuser only. The test cluster has its own bootstrap, run by the test fixture.

### Migration credentials

The web app loads `backend\.env` (ADR 0004). So migration credentials are never written to any file, and the app never sees them. `alembic/env.py` reads `MIGRATION_DATABASE_URL` from the process environment only. Set it in the shell, for one session, with a hidden prompt:

```powershell
function Set-MigrationUrl {
    $secure = Read-Host "migration_role password" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    $env:MIGRATION_DATABASE_URL = "postgresql+psycopg://migration_role:$plain@127.0.0.1:5432/grounded_answers"
}
```

Run `Set-MigrationUrl`, then the migration command, then `Remove-Item Env:MIGRATION_DATABASE_URL`. The variable lives only in that shell. Closing the window also removes it.

### Verify login

`scripts/verify_login.py` connects as each role, runs `SELECT 1`, and prints one line per role. The password is typed at a hidden prompt. Driver messages are not printed, because they can include hosts.

```powershell
uv run --directory backend python scripts/verify_login.py app_user worker_user migration_role
```

Expect `LOGIN OK app_user`, `LOGIN OK worker_user`, `LOGIN OK migration_role`, and exit code 0. A failure prints `LOGIN FAILED <role>: <error class> (sqlstate <code>)`. `28P01` means the password does not match the one in the cluster (see "Reset one role's password").

### Reset one role's password

Use this when one role's password is lost or wrong. It changes only that role. The script refuses any name except `migration_role`, `app_user` and `worker_user`.

```powershell
$env:ROLE_SCRAM = uv run --directory backend python scripts/make_scram_verifier.py app_user
Get-Content -Raw backend\scripts\reset_role_password.sql | docker compose exec -T -e ROLE_SCRAM postgres psql -U postgres -d postgres -v ON_ERROR_STOP=1 -v role=app_user -f -
if ($LASTEXITCODE -ne 0) { throw "reset failed" }
Remove-Item Env:ROLE_SCRAM
uv run --directory backend python scripts/verify_login.py app_user
```

Expect `ALTER ROLE` from psql, then `LOGIN OK app_user`. Change `app_user` in both places to reset another role. If you reset `app_user`, update `backend\.env` (step 8).

## Admin CLI (platform owner, ADR 0004, ADR 0010)

The admin CLI creates tenants, manages a tenant's allowed email domains, and creates the first tenant admin. It uses `migration_role`, so run it only on the operator's machine. It is never run in the web or worker containers.

Before the first run on a database, apply all migrations (`alembic upgrade head`, the migration steps in "Migration credentials" above). The `tenant_email_domains` table comes from migration `0003`.

```powershell
cd C:\Users\Admin\Desktop\portfolio-v2\grounded-answers\backend

# Set the migration URL for this shell only (the helper from "Migration credentials").
Set-MigrationUrl

uv run --group operator python -m grounded_answers.admin_cli create-tenant "Indus Freight Lines" indus-freight
uv run --group operator python -m grounded_answers.admin_cli add-domain indus-freight indusfreight.test
uv run --group operator python -m grounded_answers.admin_cli list-domains indus-freight
uv run --group operator python -m grounded_answers.admin_cli create-admin indus-freight admin@indusfreight.test "Head of HR"

Remove-Item Env:MIGRATION_DATABASE_URL
```

Expected output for each command, in order:

```text
Created tenant 'indus-freight' (<uuid>).
Allowed 'indusfreight.test' for tenant 'indus-freight'.
indusfreight.test
Created tenant admin (<uuid>) for tenant 'indus-freight'.
```

Rules:

- `create-admin` checks the tenant and the email domain first, before it asks for any password. A wrong domain fails at once, with no password prompt (the domain must already be allowed with `add-domain`). Then it asks for the password twice, with no echo. The two entries must match, and the password must be at least 12 characters and not on the common-password list.
- `remove-domain` removes the domain from the allowlist. Existing accounts keep working, so use it only to stop new accounts at that domain.
- A refusal prints a short reason and exits with code 1. "Nothing was changed" means the whole command rolled back.
- If the command says it must connect as `migration_role`, the URL points at another login. Fix the URL and run it again.
- `Remove-Item Env:MIGRATION_DATABASE_URL` at the end removes the URL from the shell. Closing the window also removes it.

To check the tests for the CLI: `uv run pytest -m "not db" tests/test_admin_cli.py tests/test_passwords.py`, then the DB tests with the RUNBOOK's "Running Database Tests" setup: `uv run pytest -m db tests/db/test_admin_cli.py`.

## Auth keys (ADR 0004, Session 3 Step 3a)

Access tokens are signed with Ed25519. The private keys live in one JSON file outside the repository. Only the API process reads it. The key file never goes in `.env`, and `.env.example` holds names only.

### Create the key file (first time)

```powershell
cd C:\Users\Admin\Desktop\portfolio-v2\grounded-answers\backend
New-Item -ItemType Directory -Force "$env:USERPROFILE\.grounded-answers" | Out-Null
uv run python scripts/make_jwt_keys.py --out "$env:USERPROFILE\.grounded-answers\jwt-keys.json"
```

Expected output: `Wrote C:\Users\...\jwt-keys.json with active kid ed25519-<16 hex characters>.` The script never prints key material and refuses to overwrite an existing file.

### Restrict the file to your Windows user

```powershell
$k = "$env:USERPROFILE\.grounded-answers\jwt-keys.json"
icacls $k /inheritance:r /grant:r "$($env:USERNAME):(R)"
icacls $k
```

Expected: only your account is listed, with `(R)`. Repeat this after a Windows reinstall or a new user account.

### Point the API at the file

Set these in `backend\.env` (git-ignored). Use the file path only, never the key:

```
AUTH_JWT_KEYS_FILE=C:/Users/<you>/.grounded-answers/jwt-keys.json
AUTH_LOG_HMAC_KEY=<at least 32 random characters>
```

Generate the HMAC key once, locally: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Do not paste it into chat, tickets or commits. If `AUTH_JWT_ACTIVE_KID` is set, it must equal the file's `active` value, or startup stops.

Startup fails with a message that names the setting, never its value, when the file is missing, malformed, or holds a key that is not Ed25519 or whose label does not match its public key.

### Rotate the signing key

1. Create a second file with `make_jwt_keys.py`, copy its one `keys` entry into the main file, and set `active` to the new kid. Keep the old entry.
2. Restart the API. New tokens use the new key. Old tokens still verify.
3. Wait at least 15 minutes (the access-token lifetime), then remove the old entry and restart again. Tokens signed with the removed key are refused.

The unit test `test_rotation_old_key_still_verifies_until_removed` covers this order.

### Refresh and logout (ADR 0013)

- `POST /auth/refresh` returns a new access token and a rotated `__Secure-refresh` cookie. Its `Max-Age` is
  the time left to the family's 14-day cap, not a fresh 14 days.
- **409 `refresh_in_progress`:** the same token was used less than 10 seconds ago, usually by a second tab.
  Retry after the first request finishes. The cookie is not changed.
- **401 with the cookie cleared:** the token is expired, revoked, unknown, or its family was revoked.
- **Reuse:** a token used more than 10 seconds ago is presented again. The whole family is revoked. The audit
  log shows `refresh.reuse_detected` with the number of tokens revoked. Ask the user to sign in again. Look for
  a pattern before assuming theft.
- **403 `Tenant access is suspended`:** the tenant is suspended. The family is revoked. Signing in again
  fails until the tenant is reactivated.
- Both `refresh` and `logout` refuse requests without an allowed `Origin` (403). This is expected for
  requests from tools such as `curl.exe` unless they send `-H "Origin: http://127.0.0.1:3000"`.
- `logout` and `logout-all` are idempotent: calling them again returns 204.

### Login limits and the operator unlock (ADR 0014)

- Login and refresh are limited in Valkey. Every refusal is `429` with `Retry-After` (seconds) and the body
  `Too many attempts. Try again later.`, the same for known and unknown emails.
- Login fails closed when Valkey is down: `503` with `Retry-After: 30` and a warning in the log
  (`login.limiter_unavailable action=fail_closed`). Refresh fails open and logs `refresh.limiter_unavailable`.
- The API refuses to start without `AUTH_RATE_LIMIT_HMAC_KEY` (32 characters or more). Create one the same way as
  `AUTH_LOG_HMAC_KEY` and add it to `backend\.env`:
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- **Unlock an account** (after a deliberate lockout, or when a user is locked out by mistake). Set the Valkey URL
  and the same key in this shell, then run:

  ```powershell
  $env:REDIS_URL = "redis://127.0.0.1:6379/0"
  $env:AUTH_RATE_LIMIT_HMAC_KEY = "<the value from backend\.env>"
  uv run --group operator python -m grounded_answers.admin_cli unlock-account indus-freight hr.manager@indusfreight.example
  Remove-Item Env:AUTH_RATE_LIMIT_HMAC_KEY
  ```

  Expected: `Unlocked the account in tenant 'indus-freight'. Keys removed: <n>.` The unlock is audited as
  `account.unlock` with the count of keys removed.
- **Valkey flush drops every lock.** Do not flush Valkey casually (R15).
- **Timing floor:** `AUTH_LOGIN_FLOOR_MS` (default 500, allowed 100 to 5000) sets the minimum duration of every login
  response. If the log shows `login.timing_floor_exceeded`, the real work is slower than the floor on this machine:
  raise the setting (up to 5000) and re-measure (ADR 0014, section 8). Until then, a known user's failure can be
  timed apart from an unknown email's.
- **Re-measure the timing floor** (ADR 0014, section 8) when hardware, the database or the Argon2 parameters change.
  Run the login paths 40 times each and compare the worst case with the 500 ms floor in `timing.py`.
- **Tests:** the Valkey tests use database 15 (`TEST_VALKEY_URL`, default `redis://127.0.0.1:6379/15`), flushed
  before each test. They run in the `backend-db` CI job, which starts Valkey.

### Dev cookie check

The refresh cookie is `__Secure-refresh`. Browsers keep it only when it carries `Secure`. Local development uses `http://127.0.0.1`. The browser test result and any change to the dev HTTPS setup are recorded in ADR 0012.

## Background worker (ADR 0017, Session 4 Step 1b-i)

**The sweeps (ADR 0023, 0027, 0028, as amended in PR 9).** The worker runs three sweeps in its own process, on a timer. Each is one call to its narrow definer, so the worker needs no Procrastinate periodic rights. Each sweep runs once when the worker starts, then on its interval:

| Sweep | Interval | Log line when it runs |
|---|---|---|
| stalled-version retry | every 5 minutes | `ingest.stalled_sweep enqueued=N already_waiting=M` |
| purge | every 15 minutes | `purge.sweep enqueued=N already_waiting=M` |
| queue cleanup | every 24 hours | `queue.cleanup removed=N retention_hours=168` |

Rules: a sweep never overlaps itself. A tick that arrives while the previous run is still going is skipped, and logged as `sweep.skipped`. A failure is logged as `sweep.failed name=<sweep> error=<exception class>`, with no message and no ids, and the timer keeps running. Three failures in a row log `sweep.repeated_failure ...` at warning level. A failure that repeats is for a person to look at: check the database connection and the worker window before anything else.

The worker reads `DOCUMENT_RETENTION_DAYS` (1 to 365, default 30) from the shell environment, as the web app does. The swap stamps each retirement with it, and the purge removes items when their period has passed (ADR 0027). Set it to the same value in both places.

**The web app and the worker must have the same `DOCUMENT_RETENTION_DAYS`.** A different value gives different retention periods to rows retired by the web app (delete) and by the worker (replacement swap). The worker logs its value at startup as `worker.config document_retention_days=N`: check that line against the web app's setting after every change. The swap also refuses any value outside 1 to 365 in SQL, so a bad value cannot reach a row.

The worker runs background jobs as `worker_user`. Its database URL is set in the shell only, never in a file.

**Start (from `backend\`, in PowerShell):**

```powershell
# Set the worker login for this shell only. Use the worker password you chose at bootstrap step 3.
$env:WORKER_DATABASE_URL = "postgresql://worker_user:<password>@127.0.0.1:5432/grounded_answers"
uv run python -m grounded_answers.jobs.worker
```

Expected: the process stays in the foreground. A job's outcome is logged as `Job ... ended with status: Success` or `ended with status: Error`. Job arguments are never logged.

**Stop (Windows has no graceful stop for the worker, upstream limit, ADR 0015):**

1. Find the worker's process ID: `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'grounded_answers.jobs.worker' } | Select-Object ProcessId, CommandLine`.
2. Stop that process only, by its ID: `Stop-Process -Id <ProcessId>`.

Never stop every `python.exe` process. A job that was running is halted, and it runs again later: delivery is at least once, and every job is idempotent.

**Check the worker's login without starting it:** `WORKER_DATABASE_URL` must be set in the shell. A `.env` file never provides it, and the web app refuses it if it finds one there (`tests/test_env_example.py`).

**Migration 0006 (Session 4, Step 1b-i):** a worker cannot start without it, because the worker's startup calls the stalled-worker prune. Run `uv run alembic upgrade head` once after pulling this change.

## Document upload switch (ADR 0019, Session 4 Step 3)

`POST /documents` is off by default. It answers 404 until `UPLOAD_ENABLED=true` is set in the web app's environment. Keep it off until the ingestion worker runs the task `grounded.ingest_document` (Step 6). Otherwise uploads queue jobs that nothing runs.

**Check it is off:** `curl -i -X POST http://127.0.0.1:8000/documents` returns `404` (with a valid token it still returns 404 while the switch is off).

**Turn it on (only after Step 6 is merged and the worker runs):** set `UPLOAD_ENABLED=true` in `backend\.env`, restart the API, then upload one small `.txt` as a tenant admin and expect `201`.

**Limits to know:** 10 MiB per file (streamed cap at 10 MiB + 64 KiB), 60 attempts per tenant and 20 per user per hour, `429` with `Retry-After` when exceeded, `503` when Valkey is down (fail closed).

## Text extraction limits (ADR 0020, Session 4 Step 4)

Text is extracted in a child process with these limits:

| Format | Timeout | Memory (Linux only) |
|---|---|---|
| PDF | 30 s | 1 GiB address space |
| DOCX | 10 s | 1 GiB |
| Markdown, text | 5 s | 1 GiB |

Output is capped at 50 MiB. Every limit is checked by the parent, which kills the child's whole process tree.

**Failure reasons** (the only four a document can produce; no document text is ever in them):
- `timeout`: the child ran past its time limit.
- `memory`: the child could not allocate (Linux only).
- `unreadable`: the file is not a valid file of its type, or the parser failed.
- `no_text`: the file parsed, but holds no text. A scanned PDF gives this (OCR is out of scope, SPEC §6.1).

**Logs:** a failure logs one line, `extract.failed reason=... exception=<class name>`. Nothing from the child's stdout or stderr is logged.

**Windows development:** the timeout and the output cap apply, but **there is no memory limit** on Windows (SECURITY R19). The memory test runs in the CI Linux job only, and it is skipped on Windows with that reason.

**Check the Linux limit yourself:** run the extraction tests in a Linux container, as CI does. From `backend\` in PowerShell: `docker run --rm -v "${PWD}:/work" -w /work python:3.12-slim sh -c "pip install -q uv && UV_PROJECT_ENVIRONMENT=/tmp/venv uv run -q pytest tests/test_extract_sandbox.py -rs"`. Expected: all tests pass, with the Windows-only check skipped.

## Rollback

Two things roll back separately: the application and the database schema.

**Rule: every migration must work with the previous app version.** Use expand-then-contract: add columns and tables first, remove old ones in a later release. Then an app rollback needs no schema rollback. If an older app ever meets a newer schema, `/ready` returns 503 `schema`, so the mismatch is visible at once.

**Application rollback**
1. Redeploy the previous image or commit.
2. Check `GET /ready` returns 200. A 503 `schema` means the schema head and the code no longer match. Go to schema rollback.

**Schema rollback (Alembic, as `migration_role`)**

```powershell
Set-MigrationUrl
uv run --directory backend alembic current          # expect: 0004_alembic_version_grant (head)
uv run --directory backend alembic downgrade -1     # reverts one revision
Remove-Item Env:MIGRATION_DATABASE_URL
```

Before any downgrade, check what it removes:

| Revision | Downgrade removes | Data loss | Effect on the app |
|---|---|---|---|
| 0016 | Recreates the vector index `chunks_embedding_hnsw` (a slow build on a large table) | No | Once a tenant has about 2,000 chunks the planner may choose it, and retrieval can return too few or wrong rows (ADR 0030) |
| 0004 | `app_user` SELECT on `alembic_version` | No | `/ready` reports `schema` |
| 0003 | RLS policies, definer functions and their grants | No | Tenant policies gone; RLS stays on, so with no policies every row is hidden (fail closed) |
| 0002 | Table and column grants | No | App cannot read or write |
| 0001 | Every table | **Yes, all data** | Everything |

- Never run `downgrade base` on a database that holds data.
- The round-trip test (`tests/db/test_migration_round_trip.py`) proves upgrade, downgrade and upgrade again on an empty database. It does not prove that data survives a downgrade.
- For a production rollback, take a backup first and record the backup time here.

## Branch protection (`main`)

In effect since 2026-10-22. The repo is public. Verify it with:

```powershell
gh api repos/hamzawithpython/grounded-answers/branches/main/protection --jq '{checks: .required_status_checks.contexts, reviews: .required_pull_request_reviews.required_approving_review_count, enforce_admins: .enforce_admins.enabled, force_push: .allow_force_pushes.enabled, deletions: .allow_deletions.enabled}'
```

Expected: `checks` is `["backend","backend-db","frontend","security"]`, `reviews` is `0`, `enforce_admins` is `true`, `force_push` and `deletions` are `false`.

The four required checks are the merge gate. Required approvals are `0` because this is a solo project: a PR's author cannot approve their own PR, so a required review would block every merge. Admins are enforced, so no one bypasses the checks. If a second maintainer joins, set the required approvals back to 1 and update this section and SECURITY.md (R7).

## Restore after a backup (planned with the purge job, Session 3)

A backup holds deleted content until the provider's backup window expires (SECURITY.md, ADR 0007). A restore therefore brings back documents whose purge date has passed. Until the purge job exists, **do not restore user data from a backup**.

**The purge job exists now (ADR 0027).** The restore rule is: **after any restore from backup, run the purge again before the app serves users.** A restore brings back documents and versions whose purge date has passed, and the purge removes them again. The procedure:1. Restore into a new database, never over the live one.2. Start the worker against the restored database, with `DOCUMENT_RETENTION_DAYS` set to the value the live system uses. The purge sweep runs when the worker starts and then every 15 minutes, so wait for two sweep runs (the worker logs `purge.sweep`).3. Check that nothing due is left. This query must return `0`:   ```sql   SELECT (SELECT count(*) FROM grounded.documents WHERE deleted_at IS NOT NULL AND purge_after < now())        + (SELECT count(*) FROM grounded.document_versions AS v JOIN grounded.documents AS d             ON d.tenant_id = v.tenant_id AND d.id = v.document_id           WHERE v.status = 'retired' AND v.purge_after < now() AND d.deleted_at IS NULL);   ```4. Check the audit log: each purged item has a `document.purged` or `document_version.purged` entry from the restore window.5. Only then point the app at the restored database, and check `/ready`.A purge that is still running, or a failed purge job, leaves its item in place. Look at the job queue (`queue.procrastinate_jobs`, `status`) and the worker log. Do not point the app at the database until step 3 returns `0`.

## Query embedding in the web process (ADR 0033, Step 5b)

`POST /ask` (Step 5c) needs the embedding model in the web process. It is off by default. Settings in the web app's environment (`backend\.env` or the shell):

| Setting | Default | Meaning |
|---|---|---|
| `ASK_ENABLED` | `false` | On: load the model in the background and report it in `/ready`. Off: no model, and `/ready` does not wait for one |
| `EMBEDDING_MODEL_DIR` | none | Required when `ASK_ENABLED=true`: the pinned model directory (see "Embedding model files and the worker", step 1). Startup stops with `EMBEDDING_MODEL_DIR is not set` if it is missing |
| `EMBED_CONCURRENCY` | `2` | Questions embedded at the same time, 1 to 4 (each uses two CPU threads) |

**Memory.** The web process then holds about 0.76 GB steady and 1.1 GB at the peak while loading, the same at every `EMBED_CONCURRENCY`. Web plus worker plus Postgres on one machine needs about 2.5 GB, so keep the dev database stopped for large runs.

**Check it.** Start the API with `ASK_ENABLED=true`, then call `/ready` repeatedly. Expect `503` with `{"status":"not_ready","failed":["embedder"]}` for about 10 seconds while the model loads, then `200`. If it stays 503, read the API log: `query embedder load failed reason=<ErrorClass>` (for example `ModelIntegrityError`: the model files do not match their pins, so fetch them again). The log never holds the path or the message.

**Measure again** (on a new host, or after a change to the model or its threads; about 2 minutes; needs about 1.2 GB free; do not run it beside the database tests):

```powershell
$d = "$env:LOCALAPPDATA\grounded-answers\models\614241f622f53c4eeff9890bdc4f31cfecc418b3"
uv run python -m grounded_answers.evaluation.embed_concurrency $d --report .\.eval-output\embed-concurrency.json
```

Expect a table of five cases (limit 1, 2 and 4 with as many callers, then limits 1 and 2 with four callers) and exit code 0. On a host with fewer CPUs than 4, expect the gain from 2 slots to shrink: if 2 slots give under about 20 % more throughput than 1, set `EMBED_CONCURRENCY=1`.

## Turning on `POST /ask` and the live check (ADR 0033, Step 5c-2)

**Turn it on** (in `backend\.env`, then restart the API): `ASK_ENABLED=true`, `EMBEDDING_MODEL_DIR=<the pinned model directory>` (see "Embedding model files and the worker"), and at least one provider key (`GROQ_API_KEY`). The tenant needs settings (next sections): `set-tenant-settings <slug> --data-class fictional --providers groq --daily-cap-usd 0.05`. Check `/ready`: it answers 503 `embedder` for about 10 seconds while the model loads, then 200.

**Ask a question** (PowerShell; the token comes from `/auth/login`):

```powershell
$h = @{ Authorization = "Bearer $token" }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/ask -Headers $h -ContentType "application/json" -Body (@{ question = "How many days of notice do I need to give?" } | ConvertTo-Json)
```

Expect `status` `answered` with `answer` and `citations`, or `refused` with a `reason` and a `message`. A refusal is a normal answer. An error shows as an HTTP status: 429 (limit, with `Retry-After`), 503 (`code` `not_configured`, `try_again` or `service_unavailable`) or 422 (the question: 1 to 1,000 characters, nothing else in the body). Every response has a `request_id`; the log line with the same `request_id` says what happened, and the `answer_usage` row has the tokens and cost.

**The live check** (outside CI; the 8 demo documents, the real embedding model, the real endpoint, a real answer model; about 4 minutes; needs about 1.5 GB free). Same environment as "Retrieval evaluation", then:

```powershell
$env:EVAL_ASK_LIVE = "dry"      # rehearsal: a fake model, no network, no key
uv run pytest -m eval -s -k ask_live
$env:EVAL_ASK_LIVE = "groq"     # live: real Groq, fictional data only, needs GROQ_API_KEY in backend\.env
$env:EVAL_ASK_LIMIT = "5"       # questions, at most 10
uv run pytest -m eval -s -k ask_live
```

Expect `1 passed`, a table of the questions (ids, HTTP status, result, citations, wall time) and the `answer_usage` rows (outcome, tokens, cost, latency). The live run counts against Groq's free tier (about 200,000 tokens a day; one answer is about 1,900 tokens). Ask before running it. The test rebuilds `grounded_answers_test`, so do not run it beside `pytest -m db`. It carries the `live` marker: it is the only kind of test that keeps the provider keys and the network. Every other test runs with the keys removed and a guard that fails it if it tries to leave the machine (`tests/network_guard.py`); if a new test fails with `network guard: the test tried to reach outside this machine`, give it a fake instead of marking it `live`.

## Prompt-injection defenses and the live probe (ADR 0034, Step 6)

**Migration 0018 on the dev database.** The dev database is at `0017_answer_usage_settings`. Run `alembic upgrade head` once (see "Migrations to head") after the PR that adds it is merged. It adds two codes to the CHECK list of `answer_usage.invalid_codes`; `/ready` returns 503 `schema` until then, because the code expects `0018_more_invalid_codes`.

**New refusals you may see.** A question can now end as `could_not_verify` because the model answered in a non-Latin script (`answer_language`) or recited its rules (`prompt_echo`). They are in the metering row's `invalid_codes`, and Step 7 counts them in the false-refusal rate. If real Urdu-script questions appear before v2, a reply in Urdu script will be refused: that is the stopgap in ADR 0034, decision 4.

**The live probe** (outside CI; about 4 minutes; needs about 1.5 GB free; fictional data only). Same environment as "Retrieval evaluation":

```powershell
$env:EVAL_INJECTION_LIVE = "dry"     # rehearsal and detector self-test: an obedient fake model, no network, no key
uv run pytest -m eval -s -k injection_live
$env:EVAL_INJECTION_LIVE = "groq"    # live: real Groq, about 26k tokens (at most about 53k with retries), needs GROQ_API_KEY in backend\.env
uv run pytest -m eval -s -k injection_live
```

Expect `1 passed` and a table of 12 attacks: the served result, the attempts, what validation caught (invalid codes), and which detectors fired on the served answer. The dry run must show detector hits on 9 attacks (an obedient model that cites a real passage is served: the known limit) and validation stopping the Urdu reply, the recital and the ids; that is the self-test. A live hit is a finding, not a test failure. The test rebuilds `grounded_answers_test`, so do not run it beside `pytest -m db`; it carries the `live` marker (keys and network on). Ask before running the live mode.

## `/ask` limits and the metering flag (ADR 0033, Step 5c)

Settings in the web app's environment (`backend\.env`): `ASK_RATE_USER_PER_MIN` (5), `ASK_RATE_TENANT_PER_MIN` (20), `ASK_INFLIGHT_PER_TENANT` (2, how many questions of one tenant may be in progress), `ASK_DEADLINE_SECONDS` (12, range 5 to 30, the whole request budget). A tenant's daily cap and providers are set with the admin CLI (next section).

**Valkey timeouts.** The shared Valkey client has 0.5 s socket timeouts. If Valkey is slow or down, `/ask`, login and upload answer 503 (fail closed, intended), and the API log shows `ask.valkey_unavailable operation=... reason=...`.

**The metering flag.** If writing a usage row fails twice, the log shows `ask.metering_failed`, the question still gets its answer, and `/ask` answers 503 for that tenant for 5 minutes (`ask.unavailable ... reason=metering_failed`). Find the cause before it expires, or the flag is set again by the next failed write. Likely causes: a missing grant on `answer_usage` (check with the catalog test), a full disk, the database in recovery. Look for `ask.metering_insert_failed ... reason=<ErrorClass>`.

**Log lines** (all carry `request_id` and `tenant=<16 hex characters>`, a keyed hash of the tenant id; the same tenant always gives the same value while `AUTH_LOG_HMAC_KEY` is unchanged): `ask.done` (outcome, attempts, tokens, cost in micro-dollars, milliseconds), `ask.limited`, `ask.unavailable` (stage and reason), `ask.failed` (an unexpected error: class name only). To find a tenant's value, compute it with `grounded_answers.log_ref.tenant_log_ref(key, tenant_id)`; there is no raw id in the logs to search for.

## Tenant answer settings (ADR 0033)

Every tenant needs a settings row before `/ask` (Step 5c) will answer for it. `create-tenant` makes the row with the fail-closed defaults: no providers, data class `real`, cap $0. Set the real values with the admin CLI, in the shell where `MIGRATION_DATABASE_URL` is set (see "Admin CLI"):

```powershell
uv run --group operator python -m grounded_answers.admin_cli show-tenant-settings indus-freight
uv run --group operator python -m grounded_answers.admin_cli set-tenant-settings indus-freight --data-class fictional --providers groq --daily-cap-usd 0.05
```

Expect `Updated tenant 'indus-freight': answer_providers, daily_cost_cap_micro_usd, data_class.` Options you leave out stay as they are; `--providers ""` clears the list. Run it twice and the second run says `No change` and writes no audit row.

Rules the command enforces: providers are `groq`, `gemini`, `anthropic`, no repeats; a tenant with `--data-class real` may list only approved providers (today `anthropic`), checked on the settings as they will be, so moving a tenant to `real` while `groq` is listed is refused with `Nothing changed`; the cap is in US dollars with at most 6 decimals, 0 to 1000. Every change adds an audit row `tenant_settings.update` with the old and new values. See what changed (as the cluster admin, in psql): `SELECT detail FROM grounded.audit_log WHERE action = 'tenant_settings.update' ORDER BY created_at DESC LIMIT 5;`

**Migration 0017 on the dev database.** The dev database is at `0016_drop_hnsw_index`. Run `alembic upgrade head` once (see "Migrations to head") after the PR that adds it is merged. Every existing tenant gets a settings row with the fail-closed defaults, so set the demo tenant's values with the command above. `/ready` returns 503 `schema` until the migration has run, because the code expects `0017_answer_usage_settings`.

**Metering.** `answer_usage` holds codes and numbers only. Today's spend for a tenant (as the cluster admin): `SELECT sum(cost_micro_usd) / 1000000.0 AS usd FROM grounded.answer_usage WHERE tenant_id = '<id>' AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc';`

## Deployment

TBD: Add deployment instructions for production hosting.

### Hosting checklist for file storage (ADR 0018)

Document bytes are stored in the database (`document_blobs`), so the host must be checked for these before go-live. Record each result in SECURITY.md.

1. **Storage limit.** Find the database's storage limit on the chosen host. Each file is at most 10 MiB, and the database grows with every stored version, including versions still in the retention window (ADR 0007).
2. **Backup size and window.** Find how long the host keeps backups and how big they get. A backup holds the bytes of deleted documents until it expires (ADR 0007, SECURITY R3).
3. **Encryption at rest.** Confirm the host encrypts the database disks. The application does not encrypt the bytes.
4. **Scale-up trigger.** When the database storage or backup size approaches the limit, the recorded next step is a move to object storage (Backblaze B2 is the first candidate, ADR 0018). That is a new ADR.

### Embedding model files and the worker (ADR 0022)

The worker and the database tests need the pinned model files. Production never downloads them at startup.

**1. Fetch and verify the model** (once per machine, or at image build). It downloads only missing files, checks every file's SHA-256 and size, and fails loudly on a mismatch. A file already in the cache is never replaced.

```powershell
uv run --directory backend python -m grounded_answers.embedding.fetch "$env:LOCALAPPDATA\grounded-answers\models"
```

Expect `model ready: ...\614241f622f53c4eeff9890bdc4f31cfecc418b3` and exit code 0. If it says `does not match its pin`, delete that folder and run the command again. Do not edit the files by hand: a changed file is a changed model.

**2. Set the worker's settings** in the shell, not in a file (ADR 0017):

```powershell
$env:WORKER_DATABASE_URL = "postgresql://worker_user:<password>@127.0.0.1:5432/grounded_answers"
$env:WORKER_EMBEDDING_MODEL_DIR = "$env:LOCALAPPDATA\grounded-answers\models\614241f622f53c4eeff9890bdc4f31cfecc418b3"
# Optional. Default 1200 seconds (20 minutes); allowed 120 to 7200.
$env:WORKER_PROCESSING_LEASE_SECONDS = "1200"
```

**3. Start the worker.** It refuses to start, with a clear message, if `WORKER_EMBEDDING_MODEL_DIR` is unset or a model file is missing or changed. It loads the model once, at startup. Then it checks the `worker_user` login once. A wrong password exits with `worker not started: database login failed for worker_user`, and the worker does not start. One worker process needs about 1.1 GB of RAM for the model.

```powershell
uv run --directory backend python -m grounded_answers.jobs.worker
```

**Do not start the worker against a database with queued uploads until Step 8.** Upload stays off (`UPLOAD_ENABLED=false`), but any job already queued runs as soon as the worker starts.

**Dev database, migration `0009_ingestion_lease`.** No bootstrap change in this step. Run `alembic upgrade head` (see "Migrations to head" above). The readiness check expects `0009_ingestion_lease`, so `/ready` returns 503 until the migration has run.

**Tests.** The `backend-db` job restores the model from its cache and sets `TEST_EMBEDDING_MODEL_DIR`. Locally, set it to the directory from step 1 before `pytest -m db`. The real-model test fails, and never skips, when it is unset (ADR 0006).

### Hosting checklist for vector search (ADR 0021)

The chunks table needs the pgvector extension, and the embedding model runs inside the web app and the worker. Check these before go-live and record each result in SECURITY.md.

1. **Enable pgvector.** The bootstrap SQL runs `CREATE EXTENSION IF NOT EXISTS vector` in each database. Confirm the host offers pgvector and that the extension is created, then confirm the version: `SELECT extversion FROM pg_extension WHERE extname = 'vector';`.
2. **Memory per process.** The web app and the worker each load the embedding model, about 1.1 GB of RAM per process (measured 2026-10-23 in the same conditions: 1,123 MiB peak). A host with 512 MB of RAM cannot run either process.
3. **Model files present at build time.** The pinned model is fetched once at build or deploy into the fixed cache directory. Production never downloads at startup. Confirm the image contains the files before the first start.

## Queue cleanup (ADR 0028)

The worker's timer removes **succeeded** queue jobs older than 7 days, once a day (`grounded.cleanup_finished_jobs`; the first run is at worker start). Failed, cancelled, aborted, queued and running jobs are kept. To see the queue's size, count the rows in `queue.procrastinate_jobs` by `status`. A growing `failed` count is for a person to look at, not a cleanup failure: failed jobs are kept on purpose (ADR 0028).

## Retrieval evaluation (Session 5, Step 1)

Loads the eight demo documents through the real pipeline into the **test** database (a fresh tenant), asks the 50 questions as `app_user`, and prints recall@5, MRR, latency and score tables for the vector, keyword and hybrid rankers. It takes about two minutes, because it embeds about 180 chunks with the real model. It is not part of CI: the marker `eval` is deselected unless `-m eval` is given.

Set the same variables as "Running Database Tests", plus the model directory (Embedding model files, step 1), then:

```powershell
$env:TEST_EMBEDDING_MODEL_DIR = "$env:LOCALAPPDATA\grounded-answers\models\614241f622f53c4eeff9890bdc4f31cfecc418b3"
$env:EVAL_REPORT_PATH = ".\.eval-output\retrieval-eval.json"   # optional: per-question detail, numbers and ids only
uv run pytest -m eval -s
```

Expect `1 passed, ... deselected` and the tables on screen. The run rebuilds `grounded_answers_test` first, so do not run it at the same time as `pytest -m db`. Two runs on the same files give the same numbers (latency aside). A missing `TEST_EMBEDDING_MODEL_DIR` is a failure, not a skip.

The plumbing (rankers as `app_user`, replaced versions invisible, the leak check, k rows) is covered in CI by `tests/db/test_retrieval_eval_smoke.py`, which uses a fake embedder.

## Retrieval (ADR 0030)

**Setting.** `RETRIEVAL_TOP_K` (default 5, range 1 to 20) is how many chunks `retrieve()` returns to the answer step. Set it in the environment of the web app. Step 7 compares 5 and 8.

**Migration 0016 on the dev database.** The dev database is at `0015_queue_cleanup`. Run `alembic upgrade head` once (see "Migrations to head"). It drops the vector index `chunks_embedding_hnsw`, so retrieval is an exact search of the tenant's rows. The keyword index stays. `/ready` returns 503 `schema` until the migration has run, because the code expects `0016_drop_hnsw_index`.

Check, as the admin role in psql:

```sql
SELECT indexname FROM pg_indexes WHERE schemaname = 'grounded' AND tablename = 'chunks' ORDER BY 1;
```

Expect `chunks_pkey` and `chunks_tsv_gin`, and no `chunks_embedding_hnsw`.

**When to revisit the missing vector index.** Exact search costs more as one tenant's chunk count grows (measured, vector query alone, p50 / p95: 20 / 41 ms at 2,000 chunks, 45 / 79 ms at 5,000, 114 / 193 ms at 10,000, 188 / 369 ms at 20,000; `docs/EVAL.md`). Revisit (partitioning by tenant, or an index per tenant) when either holds:
- retrieval p95 is above about 250 ms in the deployed system, or
- one tenant has more than about 10,000 chunks. Count them as the admin role: `SELECT tenant_id, count(*) FROM grounded.chunks GROUP BY 1 ORDER BY 2 DESC LIMIT 5;`

Do not add a plain HNSW index back. Once a tenant has about 2,000 chunks the planner chooses it, and it then returns fewer rows or the wrong nearest ones, because it filters after its scan (ADR 0030).

**Reproduce the evidence** (outside CI, about 20 minutes, needs the model, same environment as "Retrieval evaluation"):

```powershell
$env:EVAL_REPORT_PATH = ".\.eval-output\retrieval-scale"     # a folder; optional, one JSON file per result
uv run pytest -m eval -s -k scale
```

It builds 20 tenants with up to 28,000 chunks, prints one `GROWTH` line per tenant size and one `SETTINGS` line per pgvector setting, and puts the schema back afterwards. It rebuilds `grounded_answers_test`, so do not run it beside `pytest -m db`.

## Answer providers (ADR 0031, Session 5 Step 3b)

**Keys.** `GROQ_API_KEY` and `GEMINI_API_KEY` live in `backend/.env` only (git-ignored; check with `git check-ignore -v backend/.env`, which must print the `.gitignore` rule). Paste them yourself; never into a command line, a chat or a commit. `ANTHROPIC_API_KEY` is for a client's own paid key (BYOK) and is not set during development. A key is read as a secret and sent only in a request header. If one leaks, revoke it in the provider's console and create a new one.

**Free tiers take fictional data only.** Groq's training terms are unconfirmed and Google trains on free-tier data (ADR 0031). Never point these keys at a real client's documents.

**Run the provider test** (outside CI; live calls; same environment as "Retrieval evaluation" plus the keys):

```powershell
$env:EVAL_ANSWER_PROVIDERS = "groq"        # "groq", "gemini", or "groq,gemini"
$env:EVAL_ANSWER_LIMIT = "5"               # questions per provider; 50 is the full set
$env:EVAL_REPORT_PATH = ".\.eval-output\answer-providers.json"   # optional; numbers and ids only
uv run pytest -m eval -s -k answer_providers
```

Other knobs: `EVAL_TOKEN_BUDGET` (default 150000 tokens per provider), `EVAL_GROQ_PACE_SECONDS` (default 12, because Groq's free limit is 8,000 tokens per minute), `EVAL_GEMINI_PACE_SECONDS` (default 5). The report shows tokens used and the provider's rate-limit headers from the last response.

**Groq free-tier limits** (from the response headers of this account): 1,000 requests per day and 8,000 tokens per minute. The 200,000 tokens per day figure is from Groq's documentation and is not in the headers, so keep your own count: one full 50-question run uses about 77,000 tokens (measured, see EVAL). A 429 with a long `retry-after` means the daily limit; the runner stops instead of waiting.

**Expect:** a run stops early, with the reason in the report, when a provider rejects a request (bad key, unknown model, unsupported schema) or the token budget is reached. A missing key is a failure, not a skip.

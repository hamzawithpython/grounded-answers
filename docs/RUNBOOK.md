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
- Fix: rerun dev bootstrap steps 3 and 4 with the passwords you intend to keep, then `verify_login.py`. Keep `TEST_ADMIN_DATABASE_URL` on port 5433. The guard refuses the dev host and port, but only when `backend\.env` has a `DATABASE_URL`.

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

**4. Cluster bootstrap** (roles and passwords). Expect `ALTER ROLE` and `GRANT` lines, no `ERROR`. Safe to re-run.

```powershell
Get-Content -Raw backend\scripts\bootstrap_cluster.sql | docker compose exec -T -e MIGRATION_ROLE_SCRAM -e APP_USER_SCRAM -e WORKER_USER_SCRAM postgres psql -U postgres -d postgres -v ON_ERROR_STOP=1 -f -
if ($LASTEXITCODE -ne 0) { throw "cluster bootstrap failed" }
```

**5. Database bootstrap** (CONNECT grants, schema). Expect `GRANT` and `CREATE` lines, no `ERROR`.

```powershell
Get-Content -Raw backend\scripts\bootstrap_database.sql | docker compose exec -T postgres psql -U postgres -d grounded_answers -v ON_ERROR_STOP=1 -f -
if ($LASTEXITCODE -ne 0) { throw "database bootstrap failed" }
```

**Session 4, Step 1a (ADR 0016):** this bootstrap also creates the `queue` schema and gives `app_user` and `worker_user` the search path `grounded, queue`. On a database that was bootstrapped before this step, run step 5 again **before** `alembic upgrade head` (migration `0005`). Migration `0005` fails with "schema queue is missing" otherwise. The bootstrap is idempotent.

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

Once the purge job exists, the restore procedure is:

1. Restore into a new database, never over the live one.
2. Run the purge job in catch-up mode, for every document with `purge_after < now()`.
3. Check the audit log: each of those documents has a `document.purged` or `document_version.purged` entry.
4. Only then point the app at the restored database, and check `/ready`.

This section is a plan. Steps 2 and 3 are not testable until the purge job is written, and this section will be updated then.

## Deployment

TBD: Add deployment instructions for production hosting.

### Hosting checklist for file storage (ADR 0018)

Document bytes are stored in the database (`document_blobs`), so the host must be checked for these before go-live. Record each result in SECURITY.md.

1. **Storage limit.** Find the database's storage limit on the chosen host. Each file is at most 10 MiB, and the database grows with every stored version, including versions still in the retention window (ADR 0007).
2. **Backup size and window.** Find how long the host keeps backups and how big they get. A backup holds the bytes of deleted documents until it expires (ADR 0007, SECURITY R3).
3. **Encryption at rest.** Confirm the host encrypts the database disks. The application does not encrypt the bytes.
4. **Scale-up trigger.** When the database storage or backup size approaches the limit, the recorded next step is a move to object storage (Backblaze B2 is the first candidate, ADR 0018). That is a new ADR.

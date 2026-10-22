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
$env:PSQL_COMMAND = 'docker compose --profile test exec -T -e MIGRATION_ROLE_SCRAM -e APP_USER_SCRAM -e WORKER_USER_SCRAM postgres-test psql'

uv run pytest -m db -v          # database tests
uv run pytest -m "not db"       # unit tests (no database needed)
```

The fixture drops and recreates `grounded_answers_test` on the test cluster only. The guard refuses any other target before it connects.

**Operator bootstrap (manual, same SQL the tests run).** Passwords are typed at a hidden prompt and never appear on the command line:

```powershell
$env:APP_USER_SCRAM       = uv run --directory backend python scripts/make_scram_verifier.py app_user
$env:WORKER_USER_SCRAM    = uv run --directory backend python scripts/make_scram_verifier.py worker_user
$env:MIGRATION_ROLE_SCRAM = uv run --directory backend python scripts/make_scram_verifier.py migration_role
Get-Content -Raw backend\scripts\bootstrap_cluster.sql | docker compose exec -T -e MIGRATION_ROLE_SCRAM -e APP_USER_SCRAM -e WORKER_USER_SCRAM postgres psql -U postgres -d postgres -v ON_ERROR_STOP=1 -f -
Get-Content -Raw backend\scripts\bootstrap_database.sql | docker compose exec -T postgres psql -U postgres -d grounded_answers -v ON_ERROR_STOP=1 -f -
```

`BOOTSTRAP_ADMIN_URL` must be set for `make_scram_verifier.py`. It is the **dev** cluster's superuser on the maintenance database (`postgresql://postgres:<password>@127.0.0.1:5432/postgres`). The bootstrap runs only against the dev cluster, and the test cluster has its own bootstrap, run by the test fixture.

## Deployment

TBD: Add deployment instructions for production hosting.

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
# Edit .env.local if needed (default: http://localhost:8000)
```

### Running Services

```bash
# Start PostgreSQL and Valkey via Docker Compose
docker compose up -d

# Verify services are healthy
docker compose ps

# Backend (uses --factory to load app from create_app function, not module-level import)
cd backend
uv run uvicorn grounded_answers.main:create_app --factory --reload

# Frontend (in another terminal)
cd frontend
npm run dev
```

Visit http://localhost:3000 (frontend) and http://localhost:8000/health (backend).

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

## Deployment

TBD: Add deployment instructions for production hosting.

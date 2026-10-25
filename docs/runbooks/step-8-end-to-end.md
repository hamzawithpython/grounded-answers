# Step 8: end to end with the Indus Freight demo documents

This runbook runs the whole pipeline once, on your dev database, with the eight demo documents in `demo-data/indus-freight/`:

1. the dev database migrates to the latest head (`0015`), after the definer-role bootstrap (step 4b);
2. upload is switched on, the API and the worker run;
3. `hr.manager@indusfreight.example` uploads seven documents with `curl.exe`;
4. you watch each one become `ready` (or `failed`, with a reason);
5. Expense Policy v1.0 is replaced by v2.0, and you watch the swap;
6. read-only queries confirm the chunks, the labels, and what retrieval can see.

**Read this first.** Every command here is for you to run in your own PowerShell window. Nothing in this file was run against your dev database. Each step ends with an **Expected** block (what a healthy run prints) and a **If it fails** list.

Conventions used below:

- Run PowerShell commands from the repo root, `C:\Users\Admin\Desktop\portfolio-v2\grounded-answers`, unless a step says `backend\`.
- Secrets are typed at hidden prompts (`Read-Host -AsSecureString`) and live in the shell only. They are never written to a file, and never put on a command line.
- Every window you open for this run is a window that holds secrets. Close them at the end (step 9).

---

## Step 0: preconditions (read-only checks)

Confirm the code is what you think it is, and that nothing is already running.

```powershell
git checkout main
git pull --ff-only origin main
git log --oneline -1
```

**Expected:** the last line is `Merge pull request #46 from hamzawithpython/feat/queue-cleanup` (or a later merge that you know about). If it is older, stop and pull again.

```powershell
docker compose ps
```

**Expected:** `postgres` and `redis` (Valkey) show `(healthy)`.

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'grounded_answers' } | Select-Object ProcessId, CommandLine
```

**Expected:** no rows. If a worker or uvicorn is already running, stop it by its process ID before you continue (RUNBOOK, "Background worker", Stop). Never stop every `python.exe`.

**If it fails:** `docker compose ps` shows nothing or `unhealthy`: run `docker compose up -d postgres redis` and wait for `(healthy)`. Stop here if the stop command finds a process you do not recognise.

---

## Step 1: dev database to head (`0015`)

Two parts, in order: the definer roles (RUNBOOK step 4b, no secrets), then the migrations. Migrations `0009` to `0015` are new since your last run, so the database goes from `0009` to `0015` in this one run.

### 1a. Check where the database is now

```powershell
docker compose exec -T postgres psql -U postgres -d grounded_answers -At -c "SELECT version_num FROM grounded.alembic_version;"
```

**Expected:** one line, `0009_ingestion_lease` (the state you reported after the Step 6 fixes). If it shows `0013_replace_and_swap` or `0014_purge` or `0015_queue_cleanup`, the database is already further along: skip to 1c. If it shows anything else, stop and tell me the value.

### 1b. Definer roles (RUNBOOK step 4b)

This file creates the definer roles, and grants them to `migration_role`. It needs no secrets. It is idempotent.

```powershell
Get-Content -Raw backend\scripts\bootstrap_definer_roles.sql | docker compose exec -T postgres psql -U postgres -d postgres -v ON_ERROR_STOP=1 -f -
if ($LASTEXITCODE -ne 0) { throw "definer role bootstrap failed" }
```

**Expected:** a series of `ALTER ROLE` and `GRANT` lines, no `ERROR`, and no exception. The last line should be `GRANT` (or a `NOTICE`). The exit code is 0, so the `if` line prints nothing.

Check the four roles that migrations use (all five definers must exist):

```powershell
docker compose exec -T postgres psql -U postgres -d postgres -At -c "SELECT rolname FROM pg_roles WHERE rolname LIKE '%_definer' ORDER BY rolname;"
```

**Expected:** exactly these seven lines, in this order:

```text
auth_definer
provisioning_definer
purge_definer
queue_cleanup_definer
retention_definer
retry_definer
swap_definer
```



**If it fails:**
- `migration_role does not exist`: the login roles were never created on this cluster. Run RUNBOOK step 4a (`bootstrap_cluster.sql`, which needs the verifiers from step 3), then 4b again.
- A role is missing from the list: re-run 1b. If it is still missing, stop and send me the psql output.

### 1c. Migrations to head

```powershell
function Set-MigrationUrl {
    $secure = Read-Host "migration_role password" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    $env:MIGRATION_DATABASE_URL = "postgresql+psycopg://migration_role:$plain@127.0.0.1:5432/grounded_answers"
}
Set-MigrationUrl
uv run --directory backend alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "migration failed" }
```

**Expected:** a line for each migration that runs, the last one being `Running upgrade 0014_purge -> 0015_queue_cleanup, Queue cleanup: ...`, then no `ERROR`. The exit code is 0.

Confirm the head. `alembic current` needs `MIGRATION_DATABASE_URL`, so the variable is removed only after it runs:

```powershell
uv run --directory backend alembic current
if ($LASTEXITCODE -ne 0) { throw "alembic current failed" }
docker compose exec -T postgres psql -U postgres -d grounded_answers -At -c "SELECT version_num FROM grounded.alembic_version;"
Remove-Item Env:MIGRATION_DATABASE_URL
```

**Expected:** `0015_queue_cleanup (head)` from `alembic current`, and `0015_queue_cleanup` from psql. The last command prints nothing and removes the migration URL from this shell.

**If it fails:**
- `schema queue is missing` or `extensions` errors: a bootstrap step is missing. Run the database bootstrap for the earlier sessions (RUNBOOK "Dev database bootstrap", steps 5 and 6), then migrate again.
- `role "retry_definer" does not exist` or a similar role error: 1b did not run or did not finish. Run 1b, then 1c again.
- Any other migration error: stop, keep the output, and send it to me. Do not edit an applied migration (CLAUDE.md, migrations rule).

### 1d. Readiness and the setting

Start the API later (step 3). For now check the code's expected head against the database, with the same function the readiness route uses:

```powershell
uv run --directory backend python -c "from grounded_answers.readiness import EXPECTED_SCHEMA_HEAD; print(EXPECTED_SCHEMA_HEAD)"
```

**Expected:** `0015_queue_cleanup`, the same as the database.

Check the retention setting. It must be the same value in `backend\.env` (the web app) and in the worker's shell (step 3). Use 30 unless you chose otherwise:

```powershell
Select-String -Path backend\.env -Pattern "^DOCUMENT_RETENTION_DAYS="
```

**Expected:** `DOCUMENT_RETENTION_DAYS=30`. If the line is missing, the default (30) applies to the web app. The worker's value is set in step 3, and it must match.

---

## Step 2: the demo-data commit (separate PR)

The demo documents are committed in their own small PR, so the review is only about the fictional data. That PR adds `demo-data/README.md`, which says the company and all people are fictional. It is not part of this runbook's PR. Merge it before you run step 4, so the files are in your checkout on `main`.

```powershell
git checkout main
git pull --ff-only origin main
Test-Path demo-data\indus-freight\upload\IFL-HR-001_hr-leave-policy_v3.0.docx
```

**Expected:** `True`.

**If it fails:** `False` means the demo-data PR is not merged on `main`. Stop, and merge it before continuing.

---

## Step 3: switch upload on, start the API and the worker

### 3a. Turn upload on (`backend\.env`)

This changes one line in the web app's file. It is not a secret.

```powershell
$envFile = "backend\.env"
$lines = Get-Content $envFile
if ($lines -match "^UPLOAD_ENABLED=") {
    $lines = $lines -replace "^UPLOAD_ENABLED=.*", "UPLOAD_ENABLED=true"
} else {
    $lines += "UPLOAD_ENABLED=true"
}
# Windows PowerShell 5.1's "-Encoding utf8" writes a byte-order mark (BOM) first. Other tools may read it as part of
# the first key, so write the file as UTF-8 without a BOM.
[IO.File]::WriteAllLines((Resolve-Path $envFile), [string[]]$lines, (New-Object Text.UTF8Encoding($false)))
Select-String -Path $envFile -Pattern "^UPLOAD_ENABLED="
```

**Expected:** `UPLOAD_ENABLED=true` (one line).

**If it fails:** the line appears twice: open `backend\.env`, keep one `UPLOAD_ENABLED=` line, and run the `Select-String` again.

### 3b. Start the API (window 1)

```powershell
cd backend
uv run uvicorn grounded_answers.main:create_app --factory --loop grounded_answers.runtime:selector_loop_factory
```

Leave this window open. (The `--reload` flag from the RUNBOOK is optional here; without it, a `.env` change needs a restart, which is what this step does.)

**Expected:** `Uvicorn running on http://127.0.0.1:8000` and `Application startup complete.`

In a second window (window 2), from the repo root:

```powershell
curl.exe -s http://127.0.0.1:8000/health
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8000/ready
```

**Expected:** the health body is JSON with `"status":"ok"`, and `/ready` prints `200`.

**If it fails:**
- `/ready` returns 503 `schema`: the database is not at `0015`. Go back to step 1c.
- `/ready` returns 503 `database`: `docker compose ps` shows Postgres unhealthy. Start it, then retry.
- `Application startup complete` never appears, and the log names a setting: check `backend\.env` (the AUTH keys and `DATABASE_URL` set up in earlier sessions).

### 3c. Start the worker (window 3)

The worker needs three values in its shell: its database login, the embedding model directory, and the retention setting (the same value as the web app). None of them goes in a file.

```powershell
$secure = Read-Host "worker_user password" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
$env:WORKER_DATABASE_URL = "postgresql://worker_user:$plain@127.0.0.1:5432/grounded_answers"
$env:WORKER_EMBEDDING_MODEL_DIR = "$env:LOCALAPPDATA\grounded-answers\models\614241f622f53c4eeff9890bdc4f31cfecc418b3"
$env:DOCUMENT_RETENTION_DAYS = "30"
Remove-Variable plain, secure
cd backend
uv run python -m grounded_answers.jobs.worker
```

**Expected, in order:**
- `worker.model_loaded model=... revision=... load_ms=...` (the model loads once)
- `worker.config document_retention_days=30`
- the process stays in the foreground, with no traceback.

**If it fails:**
- `worker not started: database login failed for worker_user`: the password you typed does not match the cluster. Run RUNBOOK "Reset one role's password" for `worker_user`, then start again.
- `WORKER_EMBEDDING_MODEL_DIR is not set`, or `does not match its pin`: run RUNBOOK "Embedding model files" step 1 (`python -m grounded_answers.embedding.fetch`), then start again.
- `worker.config document_retention_days=` shows a different number from the web app's setting: stop the worker (by its PID), correct `$env:DOCUMENT_RETENTION_DAYS`, and start it again. Drift is what that line is for (RUNBOOK, Background worker).

Stop the worker only by its process ID, never by name (RUNBOOK, Stop).

---

## Step 4: log in as the HR manager (token in memory only)

The login uses a hidden prompt for the password. The token is kept in a PowerShell variable, and is never written to a file. Window 2 is the one you use from here on.

First check the account exists, with the right role. This is a read-only query (the tenant is `indus-freight`, the first tenant the RUNBOOK creates):

```powershell
docker compose exec -T postgres psql -U postgres -d grounded_answers -At -c "SELECT u.email, m.role, m.active FROM grounded.users u JOIN grounded.tenant_memberships m ON m.user_id = u.id JOIN grounded.tenants t ON t.id = m.tenant_id WHERE t.slug = 'indus-freight' AND u.email = 'hr.manager@indusfreight.example';"
```

**Expected:** one line, `hr.manager@indusfreight.example|tenant_admin|t`.

**If it fails:**
- No row: the account is not in your dev database. Create it as the first tenant admin with the admin CLI (RUNBOOK, "Admin CLI"): `create-admin indus-freight hr.manager@indusfreight.example "Head of HR"`. The domain must already be allowed (`add-domain indus-freight indusfreight.example`). Then run this check again.
- `role` is `employee`: uploads answer 403. Ask me before changing a role; it is a security decision.
- `active` is `f`: the membership is inactive; reactivate it only after we confirm why.

Log in. The password is typed at the prompt, and the request body is built in memory and piped to `curl.exe` on standard input, so the password never appears on a command line:

```powershell
$loginPassword = Read-Host "hr.manager password" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($loginPassword))
$body = @{ email = "hr.manager@indusfreight.example"; password = $plain } | ConvertTo-Json -Compress
Remove-Variable plain, loginPassword
$loginResponse = $body | curl.exe -s -X POST http://127.0.0.1:8000/auth/login -H "Content-Type: application/json" --data-binary "@-"
Remove-Variable body
$token = ($loginResponse | ConvertFrom-Json).access_token
if (-not $token) { throw "no token: check the password and the account" }
"token length: $($token.Length)"
```

**Expected:** `token length:` followed by a number (around 200 or more). Nothing else prints.

**If it fails:** `no token`: the login was refused. Read the body with `$loginResponse` (it does not contain your password). A `401` means the password or the account is wrong, and RUNBOOK "Login limits and the operator unlock" covers a locked account. Do not paste the token anywhere.

Check who you are:

```powershell
curl.exe -s http://127.0.0.1:8000/auth/me -H "Authorization: Bearer $token"
```

**Expected:** JSON with `"role":"tenant_admin"` and `"email":"hr.manager@indusfreight.example"`.

---

## Step 5: upload the seven current documents

The seven are the eight demo documents minus Expense Policy v2.0, which is the replacement in step 6. Before you upload, check that the list is empty, so a duplicate does not surprise you:

```powershell
$list = Invoke-RestMethod -Uri "http://127.0.0.1:8000/documents?limit=50" -Headers @{ Authorization = "Bearer $token" }
"documents now: $($list.items.Count)"
```

**Expected:** `documents now: 0`. If it is not zero, read `$list.items | Format-Table title, latest_version_status` and tell me before uploading: a second upload of the same bytes gets `409`, and that is expected for an identical file.

Upload each file with `curl.exe`. Each prints the HTTP status, then the JSON body. The file path is relative to the repo root.

```powershell
$files = @(
    "demo-data\indus-freight\upload\IFL-FIN-007_expense-policy_v1.0.docx",
    "demo-data\indus-freight\upload\IFL-HR-001_hr-leave-policy_v3.0.docx",
    "demo-data\indus-freight\upload\IFL-HR-002_code-of-conduct.docx",
    "demo-data\indus-freight\upload\IFL-IT-006_it-acceptable-use.md",
    "demo-data\indus-freight\upload\IFL-OPS-003_driver-handbook.pdf",
    "demo-data\indus-freight\upload\IFL-SAF-004_warehouse-safety.pdf",
    "demo-data\indus-freight\upload\IFL-SAF-005_accident-reporting.pdf"
)
$uploaded = @{}
foreach ($file in $files) {
    $raw = curl.exe -s -w "`n%{http_code}" -X POST http://127.0.0.1:8000/documents -H "Authorization: Bearer $token" -F "file=@$file"
    $status = ($raw -split "`n")[-1]
    $json = ($raw -split "`n")[0..(($raw -split "`n").Count - 2)] -join "`n"
    "$status  $(Split-Path $file -Leaf)"
    if ($status -eq "201") { $uploaded[(Split-Path $file -Leaf)] = ($json | ConvertFrom-Json).document_id }
}
"uploaded: $($uploaded.Count) of 7"
```

**Expected:** seven lines, each starting with `201`, then `uploaded: 7 of 7`. The `status` is `queued` in each response body.

**If it fails:**
- `404`: upload is still off. The API did not restart after step 3a. Restart the API window, and check `UPLOAD_ENABLED=true` in `backend\.env`.
- `403`: the account is not a tenant admin (step 4).
- `415`: the bytes are not one of the four accepted kinds (PDF, DOCX, TXT, MD). Name the file that failed.
- `413`: the file is over 10 MiB. The demo files are not.
- `429`: rate limit (60 per tenant and 20 per user per hour). Wait for the `Retry-After` seconds, then upload the rest.
- `409`: the same bytes are already a live document. The body names its `document_id`. This is fine if you uploaded it earlier; it is not an error to fix.
- `503`: Valkey is down. Run `docker compose ps`, start `redis`, and retry.

---

## Step 6: watch each document become ready (or failed)

Each document goes `queued` → `processing` → `ready`. A failed one goes to `failed`, with a reason. The list shows the latest version's status and the reason.

Poll the list every 10 seconds, until every uploaded document is `ready` or `failed`. The loop stops after 60 polls (10 minutes):

```powershell
for ($poll = 1; $poll -le 60; $poll++) {
    $list = Invoke-RestMethod -Uri "http://127.0.0.1:8000/documents?limit=50" -Headers @{ Authorization = "Bearer $token" }
    $rows = $list.items | Sort-Object title | Select-Object title, current_version_status, latest_version_no, latest_version_status, latest_failure_reason
    $rows | Format-Table -AutoSize | Out-String | Write-Host
    $pending = @($list.items | Where-Object { $_.latest_version_status -in @("queued", "processing") })
    if ($pending.Count -eq 0) { break }
    Start-Sleep -Seconds 10
}
"pending: $($pending.Count)"
```

**Expected, over time:** the table fills in. Each row goes from `queued` to `processing` to `ready`, with `current_version_status` = `ready` once the version is current, and `latest_version_no` = `1`. At the end: `pending: 0`, and every row shows `ready`.

Timing: on a CPU, a document takes seconds to a few minutes. Your demo has eight pages of text in each PDF, so allow several minutes for the whole set. If nothing changes for 5 minutes, the worker is the first thing to check: its window should show `ingest.ready` lines (each job also ends `Success` in Procrastinate's log).

**If it fails (a row reads `failed`):** the `latest_failure_reason` says why. Only these generic reasons can appear:

| Reason | Meaning | What to do |
|---|---|---|
| `unreadable` | The bytes are not the claimed kind | Check the file opens in Word or a PDF reader, then re-upload as a new version (step 6 replacement) |
| `no_text` | A PDF with no text layer (scanned) | The file needs OCR; it cannot be ingested in v1 (SPEC §6.1). Send me the title |
| `timeout` | Extraction took too long | Note the file size; send me the title |
| `memory` | Extraction ran out of memory (Linux limit; not enforced on Windows) | Send me the title |
| `embedding` | The model failed on this text | Send me the title and the worker's log line |
| `retries_exhausted` | Every attempt hit a database or lease error | Check the worker window for database errors, and `docker compose ps`; then re-enqueue (ask me; there is no button yet) |

**If it fails (the worker is not moving anything):** no change for 5 minutes, and no `ingest` line in the worker window. Check the queue (read-only):

```powershell
docker compose exec -T postgres psql -U postgres -d grounded_answers -At -c "SELECT status, count(*) FROM queue.procrastinate_jobs WHERE task_name = 'grounded.ingest_document' GROUP BY status ORDER BY status;"
```

**Expected while it runs:** `todo` and `doing` counts that fall, and `succeeded` that rises. A `todo` count that never moves means the worker is not running: start it (step 3c).

When the loop ends, record the final table in your notes. It is the baseline for step 8's verification.

---

## Step 7: replace Expense Policy v1.0 with v2.0, and watch the swap

Find the v1.0 document's id from the step 5 result, and upload the v2.0 file as a new version:

```powershell
$expenseId = $uploaded["IFL-FIN-007_expense-policy_v1.0.docx"]
"expense document: $expenseId"
$replace = curl.exe -s -w "`n%{http_code}" -X POST "http://127.0.0.1:8000/documents/$expenseId/versions" -H "Authorization: Bearer $token" -F "file=@demo-data\indus-freight\upload\IFL-FIN-007_expense-policy_v2.0.docx"
$replaceStatus = ($replace -split "`n")[-1]
$replaceBody = ($replace -split "`n")[0..(($replace -split "`n").Count - 2)] -join "`n"
"HTTP $replaceStatus"
$replaceBody
```

**Expected:** `HTTP 201`, and a body with `"version_no":2` and `"status":"queued"`. Keep the `version_id` from the body: it is the one the swap is about.

**If it fails:**
- `404` with the document id you used: the id is wrong, or the document was deleted. Check `$expenseId` against step 5.
- `409`: the bytes are identical to the current version (v1.0). That means the wrong file was used; check the file name.
- `403`: as in step 5.

Watch the list until the new version is ready. Version 1 stays current until version 2 is ready, and then the swap makes version 2 current:

```powershell
for ($poll = 1; $poll -le 60; $poll++) {
    $list = Invoke-RestMethod -Uri "http://127.0.0.1:8000/documents?limit=50" -Headers @{ Authorization = "Bearer $token" }
    $row = $list.items | Where-Object { $_.id -eq $expenseId }
    "poll $poll : latest v$($row.latest_version_no) $($row.latest_version_status), current $($row.current_version_status)"
    if ($row.latest_version_status -eq "ready" -or $row.latest_version_status -eq "failed") { break }
    Start-Sleep -Seconds 10
}
```

**Expected, over time:** `latest v2 queued`, then `latest v2 processing`, then `latest v2 ready`, with `current ready` throughout (the list cannot say which version is current; the query in step 8 does). The end state is `latest v2 ready`.

**If it fails:** `latest v2 failed` with a reason: see the table in step 6. A failed replacement leaves version 1 current (this is ADR 0024 by design), so the Expense Policy keeps answering from v1.0 until you fix it.

Confirm the swap in the database (read-only). This shows the pointer and the audit row:

```powershell
docker compose exec -T postgres psql -U postgres -d grounded_answers -At -c "SELECT d.title, v.version_no, v.status, (d.current_version_id = v.id) AS is_current, v.retired_at IS NOT NULL AS retired FROM grounded.documents d JOIN grounded.document_versions v ON v.document_id = d.id WHERE d.id = '$expenseId' ORDER BY v.version_no;"
docker compose exec -T postgres psql -U postgres -d grounded_answers -At -c "SELECT detail->>'outcome' AS outcome, detail->>'retired' AS retired FROM grounded.audit_log WHERE action = 'document.swap' AND target_id = '$expenseId' ORDER BY created_at;"
```

**Expected:** the first query shows two rows for the Expense title:

```text
IFL-FIN-007_expense-policy_v1.0.docx|1|retired|f|t
IFL-FIN-007_expense-policy_v1.0.docx|2|ready|t|f
```

(The title is the file name, and it is the same on both rows.) Row 1 is retired, not current, and retired. Row 2 is current and not retired.

The second query shows one swap row: `current|1`. Version 2's number is higher, so it becomes current, and version 1 is retired once.

**If it fails:**
- The first query shows version 1 still current: the swap did not run, or version 2 is not `ready`. Check `latest_version_status` in the list first.
- The swap audit shows `superseded`: version 2 was older than the current one. That can only happen with two replacements, so stop and tell me.
- No swap row, version 2 ready: the worker did not finish the job. Send me the worker window's last 20 lines.

---

## Step 8: verification queries

Set up the read-only checks. The tenant id is needed for the app_user query.

```powershell
$tenantId = docker compose exec -T postgres psql -U postgres -d grounded_answers -At -c "SELECT id FROM grounded.tenants WHERE slug = 'indus-freight';"
"tenant: $tenantId"
```

**Expected:** `tenant: ` followed by one UUID.

### 8a. Chunk counts per document and version (admin view)

```powershell
docker compose exec -T postgres psql -U postgres -d grounded_answers -c "SELECT d.title, v.version_no, v.status, count(c.id) AS chunks FROM grounded.documents d JOIN grounded.document_versions v ON v.document_id = d.id LEFT JOIN grounded.chunks c ON c.version_id = v.id WHERE d.tenant_id = '$tenantId' AND d.deleted_at IS NULL GROUP BY d.title, v.version_no, v.status ORDER BY d.title, v.version_no;"
```

**Expected:** one row per document and version, with `ready` rows having `chunks` above zero, and `retired` (version 1 of Expense) keeping its chunks in the database (they are hidden from retrieval, not deleted). The exact numbers are not known in advance: record them. A `ready` row with `0` chunks is a failure: send me the title.

### 8b. Clause labels present

```powershell
docker compose exec -T postgres psql -U postgres -d grounded_answers -c "SELECT d.title, count(*) AS chunks, count(*) FILTER (WHERE cardinality(c.clause_labels) > 0) AS with_clause_label FROM grounded.chunks c JOIN grounded.document_versions v ON v.id = c.version_id JOIN grounded.documents d ON d.id = v.document_id WHERE d.tenant_id = '$tenantId' GROUP BY d.title ORDER BY d.title;"
```

**Expected:** `with_clause_label` above zero for documents with numbered clauses (the policies have them). A document with `0` labels is not a failure by itself: it may have no numbered clauses. Record it, and I will check the text.

The chunks table keys on the version (it has no `document_id`), so the query joins through `document_versions`.

### 8c. What app_user can see (the retrieval rule)

Retrieval runs as `app_user` and sees only chunks of the current, ready version of a live document (ARCHITECTURE 5.6). Ask as app_user, in the tenant. The password is typed at a hidden prompt; it stays in the shell.

```powershell
$appSecure = Read-Host "app_user password" -AsSecureString
$appPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($appSecure))
docker compose exec -T -e "PGPASSWORD=$appPlain" postgres psql -h 127.0.0.1 -U app_user -d grounded_answers -c "SELECT set_config('app.current_tenant', '$tenantId', false); SELECT d.title, v.version_no, count(c.id) AS visible_chunks FROM grounded.chunks c JOIN grounded.document_versions v ON v.id = c.version_id JOIN grounded.documents d ON d.id = v.document_id GROUP BY d.title, v.version_no ORDER BY d.title, v.version_no;"
Remove-Variable appPlain, appSecure
```

**Expected:**
- Every document appears once, at its current version, with its chunk count (from 8a).
- The Expense Policy shows **only version 2**. Version 1 does not appear, because it is retired (its chunks are hidden).
- No version with status `queued`, `processing` or `failed` appears.

**If it fails:**
- `password authentication failed for user "app_user"`: the password you typed is wrong. Run `verify_login.py` (RUNBOOK) to check it.
- Version 1 of Expense appears: the retrieval rule is not working. This is a security finding: stop, keep the output, and send it to me before anything else.
- Nothing appears at all: the tenant id is wrong, or `set_config` ran in a different session. Check `$tenantId`.

### 8d. Failed versions, if any

```powershell
docker compose exec -T postgres psql -U postgres -d grounded_answers -c "SELECT d.title, v.version_no, v.status, v.failure_reason FROM grounded.document_versions v JOIN grounded.documents d ON d.id = v.document_id WHERE d.tenant_id = '$tenantId' AND v.status = 'failed' ORDER BY d.title, v.version_no;"
```

**Expected:** zero rows (no failed versions), or rows whose `failure_reason` is one of the six generic reasons in step 6. Anything else in `failure_reason` is a bug: send it to me.

### 8e. Audit trail (counts only)

```powershell
docker compose exec -T postgres psql -U postgres -d grounded_answers -c "SELECT action, count(*) FROM grounded.audit_log WHERE tenant_id = '$tenantId' AND action IN ('document.upload', 'document_version.upload', 'document.ready', 'document.swap') GROUP BY action ORDER BY action;"
```

**Expected:**

```text
     action          | count
---------------------+-------
 document.ready      |     N
 document.swap       |     1
 document.upload     |     7
 document_version.upload | 1
```

Here `N` is the number of documents that reached `ready` (7 uploads and 1 replacement that succeeded, so N is 8 when all ran). `document.swap` is 1. These are counts only: no titles, no filenames, no content.

---

## Step 9: close out

1. Stop the worker and the API, by their process IDs (RUNBOOK "Background worker", Stop). Never stop every `python.exe`.
2. Clear the variables that hold secrets:
   ```powershell
   Remove-Variable token, loginResponse, list, replace -ErrorAction SilentlyContinue
   Remove-Item Env:WORKER_DATABASE_URL -ErrorAction SilentlyContinue
   Remove-Item Env:MIGRATION_DATABASE_URL -ErrorAction SilentlyContinue
   ```
3. Close the PowerShell windows you used. Closing a window also removes its variables.
4. Record in your notes: the final list table (step 6), the chunk counts (8a), the swap rows (step 7), the failed-version result (8d), and the audit counts (8e). Send me those, and I will write the Step 8 PROGRESS entry.

**Expected:** no Python process left running, and nothing secret written to a file.

---

## Summary of what should be true at the end

- The database is at `0015_queue_cleanup` (head), and `/ready` returns 200.
- Seven documents were uploaded as `201` and each reached `ready`, or `failed` with one of the six generic reasons.
- The Expense Policy has two versions. Version 2 is current and ready; version 1 is retired.
- `app_user` sees chunks only for each document's current, ready version.
- The audit log has counts for each upload, the swap, and the ready events, and nothing else.

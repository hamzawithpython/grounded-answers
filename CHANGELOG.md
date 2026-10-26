# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Dependency and tooling changes are listed here too.

## [Unreleased]

### Added
- ADR 0031 (Session 5, Step 3): the answer-model decision. Demo on Groq gpt-oss-120b (free, fictional data); candidates Groq, Gemini 2.5 Flash-Lite and Claude Haiku 5.5; a five-point requirement for real client data; a provider-neutral `AnswerModel` interface. SECURITY gets risk R21 (document text leaves the system) and a row for the answer-model keys.
- Retrieval (Session 5, Step 2, ADR 0030): `grounded_answers.retrieval.retrieve()`, hybrid search (exact vector search plus keyword search, reciprocal rank fusion with constant 60) as `app_user` in a tenant transaction. It returns the top `RETRIEVAL_TOP_K` chunks (new setting, default 5, range 1 to 20) with text, document title, version, section, heading and clause labels. The rankers moved from `evaluation` to `retrieval`. Tests: RLS, current-version rule, tenant isolation, k rows, the no-keyword-match fallback, determinism, input validation; three planted defects were each caught.
- The evaluation harness reports recall@k and MRR@k at k = 3, 5, 8 and 10, the hybrid misses at each k, and the sensitivity to the fusion constant.
- `tests/db/test_retrieval_scale.py` (marker `eval`, outside CI): the evidence for dropping the vector index, reproducible. 20 tenants and up to 28,000 chunks; the planner's choice as one tenant grows; pgvector settings on near-duplicate content.
- Retrieval evaluation harness (Session 5, Step 1): `grounded_answers.evaluation` holds the question parsing, the hit rule, and the vector, keyword and hybrid rankers, which run as `app_user`. The report has recall@5, MRR, latency, per-type and score-distribution tables. The full run (`pytest -m eval -s`, real model, test database) is outside CI. A smoke test with a fake embedder runs in `-m db`. New pytest marker `eval`, deselected unless `-m` names it. `docs/EVAL.md` has the first results.
- Test helpers: `make_version` can add a later version to an existing document, and `FakeEmbedder` has `query_embedding`.

### Changed
- Migration `0016_drop_hnsw_index` drops the HNSW index on `chunks.embedding` (ADR 0030, amends ADR 0021 decision 3). Retrieval is an exact search of the tenant's rows. `EXPECTED_SCHEMA_HEAD` is `0016_drop_hnsw_index`; the ORM model no longer declares the index.

### Documentation
- PROGRESS: removed the stale "Dev actions before PR 3" paragraph. ARCHITECTURE section 6 now describes the chunks table (migration 0008). RUNBOOK: how to run the retrieval evaluation. EVAL: first database results.
- `docs/runbooks/step-8-end-to-end.md`: fixes from the live run (PR #49). Step 1c runs `alembic current` before the migration URL is removed. Step 3a writes `.env` without a BOM. Step 7 expects two `document.swap` rows for the Expense Policy. Step 8a counts `position` (chunks have no `id`). Step 8c no longer puts the password on the docker command line. Step 8e expects `document.swap` to equal the number of versions that became current (8).
- Step 7 decisions, ADRs 0023 to 0029: the stalled-version retry, replacement and the ordered swap, delete, the per-role transition guard, the purge job, queue cleanup and the document list. ARCHITECTURE §5 and SECURITY (definer-function rules, threats T46 and T47, R20) are updated to match. Nothing is built yet.
- ADR 0021: embedding model (multilingual-e5-small, MIT, 384 dimensions, with `passage:` / `query:` prefixes) and
  chunking (section-aware, about 1,200 characters, tables whole). Records the hosting requirement (about 1.1 GB RAM per
  process), the pinned offline model, the English-config keyword limitation for Roman Urdu, and pgvector enablement.
- `docs/EVAL.md`: retrieval evaluation method and results (45 expected clause pairs, three chunking strategies,
  keyword and vector rankers, query-time cost, a six-question Urdu mini-test).
- `docs/RUNBOOK.md`: hosting checklist for vector search (enable pgvector, memory per process, model files at build time).
- `docs/SECURITY.md`: threat model (assets, actors, trust boundaries, 24 threats with status and proving test),
  tenant-isolation layers, secrets table, backups after purge (ADR 0007), residual risks R1–R9.
- `docs/RUNBOOK.md`: health and readiness (ADR 0009); Session 2 bootstrap problems; rollback rule and
  per-revision table; branch protection with its verify command; restore procedure (planned with the purge job).
- `docs/LEARNING_LOG.md`: Session 2 concepts (RLS, FORCE, `set_config` vs `SET`, SECURITY DEFINER, SCRAM,
  composite FKs, silent REVOKE, liveness vs readiness) with interview Q&A.
- Repository made public. Branch protection on `main` applied and verified through the API (T24).

### Fixed
- The worker's log lost lines after PR #50. `worker.config` was logged under the name `__main__` (because the worker runs as `python -m`), and the purge, extraction, ingest and rowcount loggers had no handler, so `purge.sweep` and `ingest.ready` never appeared. The worker logs under `grounded_answers.jobs.worker`, and every job logger is in `JOB_LOGGERS`. A test imports the worker's whole module chain and fails on any logger without a handler. The real worker process is started in a test, and the test fails if any startup line or the ingest success line is missing (planted failures confirmed).
- The worker crashed at startup with `permission denied for function procrastinate_defer_periodic_job_v2`. Procrastinate's periodic deferrer runs as worker_user, which must not defer jobs (PR 3). The three sweeps (stalled retry, purge, queue cleanup) now run on an in-process timer, each calling its narrow definer. No periodic task is registered. An integration test runs the real worker loop as worker_user, and a planted periodic registration fails it (ADR 0023, 0027, 0028 amended; SECURITY T57).
- The extraction child process received the worker's whole environment, including `WORKER_DATABASE_URL` with the worker's password. It now starts with an allow-list (PATH, SYSTEMROOT, TEMP, TMP) and is tested for it (SECURITY.md, "Extraction child environment").
- `app_user` could set any version's status and point any document at any version of its tenant. The transition guard now allows `app_user` to retire a version and nothing else, and `app_user` loses UPDATE on `documents.current_version_id` (ADR 0026, migration `0010`). The guard's matrix test covers every status pair for both roles.
- The worker checks the `worker_user` login once at startup. A wrong password stops it with `worker not started: database login failed for worker_user` and exit code 1, instead of a retry loop that kept it running.
- The test database guard refuses the dev port (5432) unconditionally, even with no dev URL configured, and refuses a psql command that reaches a different cluster than the admin URL (same system identifier check). A test run had reset the dev cluster's login roles (LEARNING_LOG, Session 4).
- `alembic/env.py` no longer silences existing loggers (`disable_existing_loggers=False`). The default
  disabled every app logger that existed when migrations ran in the same process (Session 3, Step 2).
- `backend/.env.example` no longer contains `MIGRATION_DATABASE_URL`. The web app loads that file, and
  `Settings` forbids unknown keys, so the app failed at startup. Migration credentials now come from the
  shell only (ADR 0004). `tests/test_env_example.py` fails CI if the example and `Settings` disagree.
### Added
- Queue cleanup (ADR 0028, amended): a daily worker task removes succeeded queue jobs older than 7 days, through the definer `cleanup_finished_jobs` (migration `0015`, owner `queue_cleanup_definer`). worker_user gets EXECUTE on it and nothing else on the queue. Failed, cancelled, queued and running jobs are kept.
- Purge and retention (ADR 0027, amends ADR 0007): `DOCUMENT_RETENTION_DAYS` (1 to 365, default 30) is in Settings and the worker, and replaces both temporary constants. A periodic definer sweep defers one purge job per due item. Each job removes the item in one transaction, document row first, with content-free audit counts. Migration `0014`. The restore rule is in the RUNBOOK.
- Replacement and swap (ADR 0024): `POST /documents/{id}/versions` (tenant_admin, the same limits as upload). The new version is queued with the next version number, and its ingest job is enqueued in the same transaction. At finish, `swap_current_version` makes a newer ready version current and retires the old one, or retires a superseded version. The document row is the one serialization point, locked by replacement, the swap, and delete. Migration `0013`. The bootstrap gains the `swap_definer` role.
- Document list (ADR 0029): `GET /documents`, tenant_admin only, newest first, cursor pagination (default 20, max 50). Each item shows the current version status and the latest version (id, number, status, generic failure reason). Bad limit or cursor is 400. A failure reason is returned only when it is a generic reason.
- Document delete (ADR 0025): `DELETE /documents/{id}`, tenant_admin only. One transaction sets `deleted_at` and `purge_after`, retires every version, and writes a `document.delete` audit row with counts only. Retrieval drops the chunks on commit. A repeat delete and an unknown id both answer 404. Migration `0012` refuses a restore (clearing `deleted_at`) by a guarded role. Retention is a temporary 30-day constant until `DOCUMENT_RETENTION_DAYS` (PR 7).
- Stalled-version retry (ADR 0023, amended): a periodic sweep defers one ingest job for each version whose lease expired, through the definer function `enqueue_stalled_ingest` (migration `0011`). Duplicates are skipped per version.
- Cluster bootstrap is split. `bootstrap_cluster.sql` creates the login roles and needs the verifiers. The new `bootstrap_definer_roles.sql` creates the definer roles and needs no secrets, so adding a definer role is one command (RUNBOOK, steps 4a and 4b).
- Session 4, Step 6: ingestion worker (ADR 0022). The `grounded.ingest_document` task: claim with a lease and a fencing token; extraction, S1 chunking and embedding outside any transaction; one transaction for the chunks, the ready status, the current version and the audit row. Migration `0009_ingestion_lease` (processing columns, the claim, process and set-current policies, grants on failure_reason and the lease columns). Failure reason `embedding` added (amends ADR 0020). Dependencies: `onnxruntime==1.31.0` (MIT), `tokenizers==0.23.3` (Apache-2.0), `numpy==2.5.4` (BSD-3-Clause). fastembed was removed because it caps pillow below 13.0 (ADR 0021 amendment). The pinned model is fetched once with a SHA-256 check (`python -m grounded_answers.embedding.fetch`); the worker refuses to start on a mismatch. Worker settings: `WORKER_EMBEDDING_MODEL_DIR`, `WORKER_PROCESSING_LEASE_SECONDS`. CI caches the model by revision (`actions/cache` v6.1.0). A transition guard trigger (ADR 0022, D8) allows only the worker's status changes. A transient failure on the last attempt is recorded as `retries_exhausted`. Upload stays off until Step 8.
- Session 4, Step 5 (chunks table): migration `0008_chunks` (384-dimension embedding, English tsvector, HNSW and GIN indexes, RLS), the `extensions` schema for pgvector in the bootstrap (ADR 0021).
- Session 4, Step 4: text extraction (ADR 0020). PDF (pypdf 6.21.2, BSD-3-Clause), DOCX (python-docx 1.2.0, MIT) and Markdown and text (markdown-it-py 4.2.0, MIT) into one structure with section numbers, levels, pages and tables. Extraction runs in a child process with a timeout per format and output capped at 50 MiB; on Linux a 1 GiB address-space limit. DOCX XML entities are not resolved (XXE and billion-laughs tested). Scanned PDFs are refused as "no text found" (SPEC §6.1 limitation). Measured on the demo set; the Linux memory limit is tested in CI.
- Session 4, Step 3: upload endpoint `POST /documents` (ADR 0019), off until the ingestion worker exists (`UPLOAD_ENABLED=false`). Tenant admins only. The body is capped at 10 MiB plus framing while it streams, with or without Content-Length. The type comes from the bytes (PDF magic, DOCX parts with ZIP limits, UTF-8 text), and the extension must agree. Filenames are sanitised (path, bidi and zero-width characters removed, NFC). A duplicate of a live file returns 409 with the existing document id. The document, version, bytes, audit row and ingestion job commit together. Per-tenant and per-user rate limits fail closed. New dependency: `python-multipart` 0.0.32 (Apache-2.0).
- Session 4, Step 2b: the `ObjectStore` interface with `PostgresObjectStore` (inside the caller's transaction, no separate connection) and `InMemoryObjectStore` for unit tests (ADR 0018). A delete on a version that is not purging is refused, never counted as success. `expect_rowcount_in` accepts an allowed set of counts. One contract test runs the same cases against both stores.
- Session 4, Step 2a: document bytes table `document_blobs` (migration `0007`, ADR 0018), tenant-owned, RLS enabled and forced, with composite foreign key to the version. The web app can INSERT bytes only for a `queued` version. The worker can SELECT bytes of `queued`, `processing` and `purging` versions, and DELETE only `purging` bytes. The size check (1 to 10 MiB, matching the content) is in the database too. A DELETE needs the row visible through SELECT, so the worker's SELECT includes `purging` (ADR 0018, amendment).
- Session 4, Step 1b-ii: enqueue on the tenant transaction (outbox, ADR 0015, ADR 0017). `enqueue_job` writes the job on the session's own connection, so a rollback leaves no job. Task names must be on an allowlist (empty until the ingestion task), and arguments must be UUIDs. The web app and the worker install the same argument-redaction filter on the job loggers. Poison-job rule: a job for another tenant's document fails once, with no retry and no ids in the logs (`load_document_for_worker`, `DocumentNotFoundForTenant`).
- Session 4, Step 1b-i: worker process (ADR 0017). `WorkerSettings` reads `WORKER_DATABASE_URL` from the environment only; the web app refuses it in a `.env` file. The worker runs as `worker_user` on the selector loop. `begin_worker_transaction` sets the tenant only. A logging filter blanks job arguments in every Procrastinate log record and keeps each job's outcome. Migration `0006` grants `worker_user` the stalled-worker prune that every worker calls at startup.
- Session 4, Step 1a: job queue schema (migration `0005`, Procrastinate 3.10.0, MIT, pinned in `pyproject.toml`).
  The queue lives in schema `queue`, created by the bootstrap and owned by `migration_role` (ADR 0016). Grants
  are the exact list the code paths need, reviewed in `tests/db/expected_queue_grants.json`. PUBLIC has no
  privilege on any queue object. Tests: connector finds the queue with `queue` on the path and not without it;
  `app_user` enqueues one job with one event and cannot read its arguments. Enqueue grants for `app_user`:
  `SELECT (id)` on jobs, `INSERT` on events, and the events sequence (ADR 0016, R17 accepted).
- Session 3, Step 4: background job runner decided (ADR 0015): Procrastinate 3.10.0 on PostgreSQL. A throwaway
  spike proved that a document row and its job commit or roll back together. Job arguments are ids only; delivery is
  at least once, so every job is idempotent; the worker treats the job's tenant id as untrusted.
- Session 3, Step 3c: login protection in Valkey (ADR 0014). Email+IP pair lock, per-IP limit, progressive
  delay per account, 24-hour hard lock released by `unlock-account`, refresh limit per IP. Atomic Lua counters,
  HMAC'd keys with expiry, fail closed on login and open on refresh (with logged warnings). Every login response
  waits for a 500 ms floor (R11), set from a measured worst case of 299 ms. New setting `AUTH_RATE_LIMIT_HMAC_KEY`.
  The operator CLI gains `unlock-account`.
- Session 3, Step 3b: refresh rotation with reuse detection, logout and logout-all (ADR 0013). The rotation
  takes a row lock (`SELECT ... FOR UPDATE`), and a race test proves that two concurrent refreshes with one
  token rotate once. Reuse after the 10-second grace window revokes the family. Family lifetime is capped at
  14 days from its first token. `POST /auth/refresh` and `POST /auth/logout` require an allowed Origin.
  `rowcount.affected_rows` added for writes that may legitimately affect zero rows.
- Session 3, Step 3a: authentication. `POST /auth/login` (generic 401, byte-identical bodies), `GET /auth/me`
  (per-request membership, user and tenant checks; role from the database). EdDSA access tokens (15 minutes,
  pinned algorithm, kid, required claims, 5-second leeway). Refresh token issued and stored hashed; cookie
  `__Secure-refresh` (HttpOnly, Secure, SameSite=Lax, Path=/auth). Argon2 verify in a worker thread. 422
  responses no longer echo input values. `scripts/make_jwt_keys.py` (key file, outside the repo). ADR 0012.
  Dependencies: `PyJWT[crypto]>=2.15.1` (MIT), `cryptography>=50.0.2` (Apache-2.0 OR BSD-3-Clause).
- Session 3, Step 2: ORM models for the nine tables (`models.py`), mirroring the migrations. Names checked
  by Alembic's comparison; CHECK names and definitions in `tests/db/expected_checks.json`.
- `tenant_db.py`: `begin_tenant_transaction` (the only way to start a tenant transaction; sets tenant and user
  with `set_config(..., true)` as its first statements) and `make_tenant_session` (FastAPI dependency,
  `scope="function"`, so the commit happens before the response). ADR 0011.
- `rowcount.py`: `expect_rowcount` for UPDATE and DELETE (SECURITY R9). Logs no row data.
- `database.py`: `create_session_factory` (`expire_on_commit=False`); the app lifespan sets `app.state.session_factory`.
- Tests: pooled-connection leak on one physical connection (R8), `lazy="raise"`, deferred columns,
  drift check, failed-commit behaviour, rowcount. Unit 85, DB 116 passing.
- Session 3, Step 1: admin CLI (`python -m grounded_answers.admin_cli`, run with `uv run --group operator`).
  Commands: `create-tenant`, `add-domain`, `remove-domain`, `list-domains`, `create-admin`. Uses
  `migration_role` from the shell only; every command is one transaction with an audit row (ADR 0010).
  `create-admin` checks the tenant and the email domain before it asks for a password.
- `grounded_answers/passwords.py`: password policy (12 to 256 characters, 10k common-password list) and
  Argon2id hashing with the pinned library defaults (ADR 0010). List vendored from SecLists (MIT), see `data/README.md`.
- Dependencies: `argon2-cffi>=25.1.0` (runtime, ADR 0004); `click>=8.5.0` in the `operator` group (ADR 0010).
  Transitive: `argon2-cffi-bindings`, `cffi`, `pycparser`. CI installs the `operator` group.
- ADR 0010: admin CLI (Click) and password hashing (argon2-cffi).
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

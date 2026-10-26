# Security — Threat Model and Controls

**Last updated:** 2026-10-22 (Session 3, Step 3c: login protection in Valkey, timing floor; ADR 0014). Status words are used exactly:
**Built + tested** (code exists and a test proves it), **Designed** (decided in an ADR, not yet
in code), **Not started**.

---

## Known Vulnerabilities & Mitigation

### npm: braces vulnerability (GHSA-vfj7-8cjw-p6xm)

**Issue:** The `braces` npm package (v3.0.3 and earlier) is vulnerable to stack overflow via deeply nested brace patterns. It's a transitive dev-only dependency: `eslint-config-next` → `fast-glob` → `micromatch` → `braces`.

**Advisory:** https://github.com/advisories/GHSA-vfj7-8cjw-p6xm
**Severity:** High (CVSS 8.7)
**Status:** No patched version available yet (as of 2026-10-22)

**Mitigation:**
- Braces is a dev-only dependency, not used in production
- CI uses `npm audit --omit=dev --audit-level=high` (blocking step that excludes dev vulnerabilities)
- Separate non-blocking `npm audit` step for visibility/tracking
- No downgrade or `--force` flag used; we wait for upstream patch

**Action items:**
- [ ] Monitor braces releases: https://www.npmjs.com/package/braces
- [ ] Once patched, upgrade eslint-config-next or braces directly
- **Review date:** 2026-11-22 (one month)

### npm: ESLint plugin peer ranges below ESLint 10 (dev-only, not a vulnerability)

**Issue:** `eslint-config-next@16.4.2` bundles `eslint-plugin-react`, `eslint-plugin-jsx-a11y` and `eslint-plugin-import`. Their peer ranges stop at ESLint 9, so `npm ls eslint` reports them as invalid. `npm ci` still succeeds (npm prints `ERESOLVE overriding peer dependency` warnings).

**Verified:** 2026-10-22. Lint runs and reports violations from all three plugins (planted `rules-of-hooks`, `jsx-a11y/alt-text`, and `react-hooks` cases). No ESLint 10 breakage found.

**Risk:** dev-only (lint tooling). A plugin could break on a future ESLint 10 minor release without a peer warning.

**Action items:**
- [ ] Re-check when `eslint-config-next` declares ESLint 10 peer support (`npm view eslint-config-next@latest peerDependencies`)
- **Review date:** 2026-12-01

---

## Threat Model

### Scope

A multi-tenant document Q&A API. Each tenant is a company. Users belong to one tenant (single-membership rule, ADR 0004). Tenant documents are stored and used to answer questions.

### Assets

| Asset | Why it matters |
|---|---|
| Tenant documents and their text | Confidential to one tenant. Leaking one tenant's content to another is the worst case. |
| Users' identities and password hashes | Account takeover. Hashes must never be readable by the web app. |
| Refresh tokens | Session takeover. Stored only as SHA-256 hashes. |
| Migration and admin credentials | Schema change or cross-tenant access. Never in the web app's environment. |
| Audit log | Proof of who did what. Must not contain document content or secrets. |

### Actors and trust boundaries

| Actor | Trust level | Reaches |
|---|---|---|
| Anonymous internet user | None | `/health`, `/ready`, login (later) |
| Authenticated tenant member | Scoped to one tenant | Its own tenant's rows (RLS) |
| Tenant admin | Scoped to one tenant, more rights | Its tenant's users (`create_tenant_user`) |
| Purge worker (`worker_user`) | Process, not a person | Narrow cross-tenant work, through RLS and definer functions |
| Platform owner / operator | Full, through the admin CLI | Migrations, tenant creation, allowlists (`migration_role`) |
| Database superuser | Full | Bootstrap and tests only; never used by the app |

Trust boundaries: browser ↔ API (HTTP, CORS allowlist); API ↔ PostgreSQL (`app_user`, RLS); worker ↔ PostgreSQL (`worker_user`, RLS); operator ↔ PostgreSQL (`migration_role`, shell-only credentials).

### Threats and controls

| # | Threat | Control | Status | Proof |
|---|---|---|---|---|
| T1 | Tenant A reads tenant B's rows through the API | RLS on every table, forced, with `app.current_tenant` set per transaction | Built + tested | `test_policies`, `test_fail_closed` (`test_tenant_context_sees_exactly_its_own_rows`) |
| T2 | A request with no tenant context reads anything | Policies match nothing when the setting is NULL (fail closed) | Built + tested | `test_fail_closed` (`test_no_tenant_context_sees_nothing`) |
| T3 | Tenant A writes a row into tenant B | `WITH CHECK` on every INSERT policy names the session tenant; grants limit columns | Built + tested | `test_rls_insert` (refused by RLS, refused by grant, `audit_log` with NULL actor), `test_policies` (`test_every_app_and_worker_insert_policy_checks_the_tenant`) |
| T4 | Password hash read by the web app | `app_user` has no SELECT on `password_hash`; the ORM defers it | Built + tested | `test_fail_closed` (`test_app_user_cannot_read_password_hash`), `test_catalog` (exact grants list) |
| T5 | A role gains superuser or RLS bypass | Login roles are NOSUPERUSER, NOBYPASSRLS, NOINHERIT; `FORCE` RLS on all tables except `alembic_version` | Built + tested | `test_catalog` (role attributes, RLS forced) |
| T6 | Definer function used to read or write too much | `SECURITY DEFINER` with pinned `search_path`, schema-qualified names, no EXECUTE for PUBLIC, `NOLOGIN` owner roles | Built + tested | `test_catalog` (search_path pinned, no PUBLIC execute, exact routine grants) |
| T7 | Unexpected privilege appears after a migration | Exact grants list, compared on every DB test run | Built + tested | `test_catalog` (`test_grants_on_grounded_match_expected_list`) |
| T8 | Tenant admin creates users outside its tenant or domains | `create_tenant_user` takes tenant and actor from the session, checks the admin role and the domain allowlist; all refusals look identical | Built + tested | `test_create_tenant_user` (`test_every_refusal_looks_identical_to_the_caller`, `test_actor_cannot_be_passed_as_an_argument`) |
| T9 | Purge worker deletes a live document, or another tenant's data | Worker transitions only `retired → purging → deleted`; deletes only purging versions and deleted documents with no versions; RLS scopes it to the tenant | Built + tested | `test_worker_policies` (13 tests, including cross-tenant refusals) |
| T10 | Worker reads identity or token tables | No grants on identity or token tables | Built + tested | `test_worker_policies` (`test_worker_has_no_access_to_identity_or_tokens`) |
| T11 | Stolen or guessed database password | SCRAM-SHA-256 verifiers, computed client-side by libpq; random per-session passwords in tests; password policy (≥16 ASCII chars) | Built + tested | `test_verifier`, `test_login` (`test_wrong_password_is_refused`, `test_definer_roles_cannot_log_in`) |
| T12 | Test or dev run resets the dev cluster's role passwords | Separate test cluster (port 5433); guard refuses an admin URL on a dev host and port | Built + tested | `test_guard` (`test_admin_on_dev_port_with_other_user_is_refused`, `test_admin_on_dev_host_via_localhost_is_refused`) |
| T13 | Migration credentials reach the web app | Migration URL is shell-only; `Settings` forbids unknown keys; the example file is tested | Built + tested | `test_env_example` (`test_settings_rejects_unknown_key_in_env_file`) |
| T14 | Migration or bootstrap changes a live schema silently | Migrations are explicit SQL, applied in order, reversible; round-trip test; `EXPECTED_SCHEMA_HEAD` checked by `/ready` | Built + tested | `test_migration_round_trip`, `test_schema_head`, `test_ready` (`schema behind code`) |
| T15 | Token forgery, algorithm confusion, refresh-token theft | EdDSA pinned, `kid` selects the key, required claims, iss and aud checked (3a). Refresh tokens hashed; rotated on every use under a row lock; reuse after the 10-second grace window revokes the family; absolute 14-day family cap (3b, ADR 0013). | Built + tested (ADR 0012, ADR 0013) | `test_auth_tokens`, `test_auth_flows`, `test_auth_refresh` (`test_reuse_after_the_grace_window_revokes_the_family`, `test_two_concurrent_refreshes_with_one_token_rotate_exactly_once`, `test_rotated_cookie_max_age_is_the_time_left_to_the_family_cap`) |
| T16 | Password guessing and spraying | Email+IP pair lock (5 failures, 15 min); per-IP limit (20 failures, 10 min); progressive delay per account (1 to 30 s); hard lock per account (50 failures, 24 h, operator release). Atomic Lua counters, HMAC'd keys, every key expires. Refused attempts are not counted. Dummy Argon2 hash for unknown emails. Limits run before the lookup, so known and unknown emails get the same answer. | Built + tested (ADR 0014) | `test_rate_limit` (concurrent failures counted exactly; pair lock per IP; progressive delay; hard lock and operator release; per-IP limit; keys hide raw values; keys expire), `test_auth_limits` (`test_fifty_failures_give_identical_responses_for_known_and_unknown_emails`, `test_valkey_down_fails_login_closed_with_a_logged_warning`), `test_auth_flows` (dummy hash path) |
| T17 | Tenant context leaks between pooled connections | `set_config(..., true)` as the first two statements of every tenant transaction (tenant and user); `begin_tenant_transaction` is the only path; a source test forbids other setters; a fresh session per transaction | Built + tested | `test_tenant_transaction` (commit, rollback and mid-transaction error on one physical connection, `pg_backend_pid` checked; statement order and bound values), `test_tenant_context_source` (only `tenant_db.py` sets the context). The commit test fails if the helper uses session scope (checked by mutation). |
| T18 | Lazy loading returns a silent partial result | `lazy="raise"` on every relationship; `deferred` on `users.password_hash` and `updated_at`; models mirror the migrations, checked by a drift test | Built + tested | `test_models` (every relationship is `lazy="raise"`; deferred columns; no CHECK in models), `test_orm_models` (unloaded relationship raises with no query; `select(User)` as app_user omits `password_hash`; explicit load refused), `test_orm_metadata` (Alembic comparison, plus CHECK names and definitions against `expected_checks.json`; planted drift is caught) |
| T19 | Public readiness endpoint leaks internal details | 503 names only the dependency; log has exception class names only; 2 s cache limits load | Built + tested | `test_ready` (`test_failure_detail_is_logged_without_secrets`), `test_readiness_db` |
| T20 | Brute-force or resource exhaustion on `/ready` | Result cached 2 s; checks under 1 s timeout each and a 2 s budget | Built + tested | `test_ready` (`test_repeated_calls_within_ttl_do_not_recheck`, timeout tests) |
| T21 | Secrets committed to git | `.env` git-ignored; gitleaks in CI; test-only secrets marked as such | Built | CI `security` job |
| T22 | Dependency with a known vulnerability | Dependabot with a 7-day cooldown; `pip-audit` in CI; `npm audit` | Built | CI jobs `backend`, `frontend`, `security` |
| T23 | Cross-site request forgery on refresh and logout | Cookie is HttpOnly, Secure, SameSite=Lax, Path=/auth. `POST /auth/refresh` and `POST /auth/logout` require an Origin on the CORS allowlist (ADR 0013 D6). `logout-all` uses a bearer token. | Built + tested (ADR 0013) | `test_refresh_and_logout_refuse_a_missing_or_foreign_origin`, `test_login_issues_an_access_token_and_a_hardened_refresh_cookie` |
| T24 | Unauthorised change to `main` | Repo is public; `main` requires 4 status checks (`backend`, `backend-db`, `frontend`, `security`), strict up-to-date checks, admins enforced, no force pushes or deletions. Required approvals are 0 (solo maintainer; see R7). | Built (verified via the branch protection API) | RUNBOOK, "Branch protection" (the verify command) |
| T25 | Migration credential leaks through the admin CLI (argument, shell history, `.env`, or a wrong login) | Migration URL from the process environment only; hidden prompt when the URL has no password; CLI never reads `backend/.env`; passwords are never arguments; CLI refuses any login other than `migration_role`; errors show the exception class, not driver text | Built + tested | `tests/test_admin_cli.py` (`test_missing_migration_url_is_refused`, `test_invalid_url_message_does_not_echo_the_url`, `test_url_without_password_prompts_for_it`), `tests/db/test_admin_cli.py` (`test_cli_refuses_to_run_as_app_user`) |
| T26 | Weak first-admin password, or a password hash that leaks or is weak | Policy: 12 to 256 characters, not on the 10k common-password list, typed twice; Argon2id with the pinned defaults (ADR 0010); hash stored, plaintext never stored or logged; `app_user` cannot read `password_hash` | Built + tested | `tests/test_passwords.py` (policy and pinned Argon2 parameters), `tests/db/test_admin_cli.py` (`test_create_admin_writes_user_membership_and_audit`, `test_app_user_cannot_read_the_password_hash`) |
| T27 | Job arguments (tenant and document ids) written to logs, through Procrastinate's messages and structured fields | Redaction filter on the worker's handler (ADR 0017, decision 4): every extra field at any depth, and the message, with values blanked and keys and outcomes kept | Built + tested | `tests/test_job_logging.py` (message, `job`, `jobs`, nested), `tests/db/test_worker_run.py` (every attribute of every record, for a failing and a succeeding job) |
| T28 | The worker's database login reaches the web app, and the web app can then delete documents | `WORKER_DATABASE_URL` only in the worker's environment; never in a file; the web `Settings` refuses it in a `.env` file (ADR 0017, decision 1) | Built + tested | `tests/test_worker_config.py` (no `.env` read; missing URL refused), `tests/test_env_example.py` (`test_web_settings_rejects_a_worker_url_in_the_env_file`, `test_env_example_has_no_worker_credentials`) |
| T29 | The tenant context of one job reaches the next job on the same pooled connection | `begin_worker_transaction` sets the tenant only, with `set_config(..., true)` first; the source scan allows only `tenant_db.py` to set it (ADR 0017, decision 3) | Built + tested | `tests/db/test_worker_transaction.py` (rollback and commit on one backend; RLS shows only the job's tenant), `tests/test_tenant_context_source.py` (`test_the_worker_helper_sets_the_tenant_only`) |
| T30 | A job with content or a foreign id is queued (content in the queue, or a malformed argument reaches the worker) | `enqueue_job` refuses any task name not on `ALLOWED_TASKS` and any argument that is not a `uuid.UUID`, before writing; the refusal names the rule, never the value (ADR 0015 decision 3, ADR 0017) | Built + tested | `tests/test_enqueue_validation.py` (unknown task, str/int/bytes/None/list/dict refused, value never echoed), `tests/db/test_enqueue.py` (refused writes nothing; stored args are canonical UUID strings) |
| T31 | A job for another tenant's document runs and reads or changes that document | The worker reads the document under the job's own tenant; a document outside it raises `DocumentNotFoundForTenant`; the job fails once, with no retry; the log holds no ids (ADR 0015 decision 9) | Built + tested | `tests/db/test_poison_job.py` (refused once, failed, no ids in logs; own tenant succeeds; helper refuses directly) |
| T32 | A job is written outside its transaction, so a document can exist without its job, or a job without its document | `enqueue_job` writes on the caller's driver connection, inside `begin_tenant_transaction` (ADR 0015 outbox) | Built + tested | `tests/db/test_enqueue.py` (committed keeps the job; rolled back leaves none). A planted separate-connection enqueue made the rollback test fail, then was removed. |
| T33 | An upload too large for memory or disk, or a body that never ends | `UploadBodyLimit` counts the body as it streams, with or without Content-Length, and stops it at 10 MiB + 64 KiB before any parser runs; form limits on fields and files (ADR 0019, D2) | Built + tested | `tests/test_upload_pure.py` (declared and chunked overflow refused; a body under the cap passes; other paths untouched), `tests/db/test_upload_endpoint.py` (chunked body without Content-Length gives 413; a file over 10 MiB; many fields; two files) |
| T34 | A file that is not the type its name or label claims (fake PDF, renamed PDF, ZIP that is not a DOCX, binary text) | Type from the bytes, the extension must agree; the client's Content-Type is ignored; DOCX parts and ZIP limits, no extraction to disk; UTF-8 and NUL checks for text (ADR 0019, D3) | Built + tested | `tests/test_upload_pure.py` (magic, renamed PDF, missing DOCX parts, zip bomb limits, encrypted entry, bounded read, non-UTF-8, NUL), `tests/db/test_upload_endpoint.py` (415 with a generic body; the label is ignored) |
| T35 | Display or path tricks in the filename (path traversal, right-to-left override, zero-width characters) | Last path segment only; control, bidirectional and zero-width characters removed; NFC; length limit; the name is never a storage key (ADR 0019, D4) | Built + tested | `tests/test_upload_pure.py` (traversal, RTL override, zero-width, NFC), `tests/db/test_upload_endpoint.py` (stored names are clean) |
| T36 | An upload by an employee, or a flood of uploads from one tenant or user | Only `tenant_admin`, read from the database; per-tenant and per-user rate limits in Valkey; fail closed with 503 (ADR 0019, D8) | Built + tested | `tests/db/test_upload_endpoint.py` (employee 403, no token 401, per-tenant 429 with Retry-After, per-user 429, Valkey down gives 503) |
| T37 | A failed upload leaves a document, bytes, an audit row or a job behind | One tenant transaction for all of them; the rollback test forces a failure at the last step (ADR 0019, D6) | Built + tested | `tests/db/test_upload_endpoint.py` (`test_a_failure_at_the_last_step_keeps_nothing`), `tests/db/test_postgres_store_outbox.py` |
| T38 | XML entity expansion or external entity (XXE) in a DOCX reads a local file or blows up memory | python-docx parses with `resolve_entities=False` on both parsers (verified in the source, ADR 0020 decision 4); no entity is resolved | Built + tested | `tests/test_extract_model_markdown_docx.py` (XXE with a canary file: no canary text; billion laughs: refused or not expanded; a control parser with entities resolved does leak the canary, so the check is sensitive) |
| T39 | A hostile PDF or DOCX makes extraction run forever or exhaust memory | Extraction in a child process: a timeout per format (30 s PDF, 10 s DOCX, 5 s Markdown and text), with the whole process tree killed; on Linux a 1 GiB address-space limit (`RLIMIT_AS`); pypdf's own decompression limit (ADR 0020, decisions D-D and section 3) | Built + tested (timeout on all platforms; memory on Linux in CI) | `tests/test_extract_sandbox.py` (timeout kills a sleeping child; a real deep-nesting file ends as timeout or unreadable; a stdlib decompression bomb under 256 MiB is a memory failure on Linux; the real bomb file is refused as unreadable) |
| T40 | Document text leaks through a child's stderr, a traceback or a failure reason into logs or responses | Stderr is read and discarded; the child writes one JSON object and reports only the exception's class name; every failure maps to one of four generic reasons; the log line carries the reason and the class name only (ADR 0020, the stderr rule) | Built + tested | `tests/test_extract_sandbox.py` (a child that writes a marker to stderr; a real document carrying the marker; the marker appears in neither the reason nor the logs) |
| T41 | A child process floods the parent with output | Output capped at 50 MiB, read through a bounded reader; overflow stops the child and the tree at once (ADR 0020) | Built + tested | `tests/test_extract_sandbox.py` (`test_a_child_that_writes_too_much_output_is_stopped_and_refused`) |
| T42 | A tampered or corrupted embedding model changes the vectors without anyone noticing | Every model file is pinned by SHA-256 and size; the fetch step hashes each download before keeping it and never replaces a cached file; the worker refuses to start on a mismatch; CI checks the restored cache (ADR 0022, D7) | Built + tested | `tests/test_ingest_units.py` (verification and fetch refusal tests); `tests/db/test_embedding_model.py` |
| T43 | Two workers write the same document version, or a slow worker overwrites a newer result | Lease with a fencing token: the final write needs the token its attempt claimed; a worker that lost its lease writes nothing and rolls back (ADR 0022, D1) | Built + tested | `tests/db/test_ingestion.py` (`test_an_older_worker_that_lost_its_lease_writes_nothing`, `test_a_lost_lease_cannot_mark_the_version_failed`) |
| T44 | Model or parser error text carries document text into the database or the logs | Only the generic reason is stored; the model's exception class name is logged, never its message (ADR 0020, ADR 0022, D4) | Built + tested | `tests/db/test_ingestion.py` (`test_an_embedding_failure_is_recorded_as_embedding_without_its_message`, `test_no_document_text_reaches_the_logs_on_success`) |
| T45 | A worker, or a bug in it, moves a version to a status the lifecycle does not allow (for example queued straight to ready, which skips the claim and the lease) | A BEFORE UPDATE trigger allows only the worker's steps (queued to processing, processing to ready or failed, retired to purging); any other change by `worker_user` is refused. Extended per role in Step 7 (ADR 0022, D8) | Built + tested | `tests/db/test_ingestion.py` (`test_the_worker_transition_guard_refuses_every_step_but_its_own`) |
| T46 | `app_user` (an app bug) moves a version to a status it may not reach, or points a document at another version | Per-role transition guard (`app_user` may only retire), and app_user loses UPDATE on `documents.current_version_id` (ADR 0026, migration 0010) | Built + tested | `tests/db/test_transition_guard.py` (the full allowed and forbidden matrix for each role; `test_app_user_cannot_point_a_document_at_a_version`) |
| T47 | A definer function used to change another tenant's data, or a role's own rights are widened | Definer rules in "Definer functions" above; the owner is listed in the guard with only its transitions (ADR 0023, 0024, 0026) | Partly built (PR 3: `enqueue_stalled_ingest`); planned PR 6 (`swap_current_version`) | Catalog test; `tests/db/test_stalled_retry.py` (owner, search_path, worker cannot enqueue except through the function) |
| T48 | The extraction child (which parses untrusted documents) reads the worker's secrets from its environment | The child starts with an allow-list environment (PATH, SYSTEMROOT, TEMP, TMP) and `python -I` (SECURITY.md, "Extraction child environment") | Built + tested (PR 3) | `tests/test_child_environment.py` (`test_the_child_process_sees_no_worker_or_auth_variables`, plus a regression test that the launcher passes env=) |
| T49 | A deleted document is brought back by clearing `deleted_at` (a restore that returns its chunks to retrieval) | A BEFORE UPDATE trigger refuses it for `app_user` and `worker_user`; the delete path sets `deleted_at` once (ADR 0025, migration 0012) | Built + tested (PR 4) | `tests/db/test_document_delete.py` (`test_app_user_cannot_restore_a_deleted_document`) |
| T50 | A worker finishing a version after its document was deleted writes chunks that retrieval then shows | The delete retires the version; the finish needs `status = 'processing'` and the fencing token, so it matches zero rows and writes nothing (ADR 0022, ADR 0025) | Built + tested (PR 4) | `tests/db/test_document_delete.py` (`test_a_worker_finishing_a_deleted_version_loses_cleanly`) |
| T51 | The document list returns stored free text, such as a raw error, that a generic-reason rule should have replaced | Failure reasons leave the server only if they are in `FAILURE_REASONS`; anything else is null (ADR 0029) | Built + tested (PR 5) | `tests/db/test_document_list.py` (`test_stored_text_that_is_not_a_generic_reason_is_never_returned`, `test_each_generic_failure_reason_is_shown_as_it_is`) |
| T52 | Two replacements, or a replacement and a delete, race on version numbers or on the current pointer | One serialization point per document: the document row, locked FOR UPDATE by replacement, the swap and delete, and by the finish before any version write (lock order: document, then versions) (ADR 0024) | Built + tested (PR 6) | `tests/db/test_replace_race.py` (two finishing together; newer first; retrieval atomicity; deadlock ordering), `tests/db/test_replace_version.py` (two uploads at once) |
| T53 | The purge removes data before its retention period, or a half-purged item is left visible | The purge checks due-ness under the document lock; one transaction per item, so a failure rolls back and a re-run completes; a retired version of a live document is never the current one (ADR 0027) | Built + tested (PR 7) | `tests/db/test_purge.py` (nothing before purge_after; crash rolls back and re-run completes; re-run is a no-op; the current version untouched) |
| T54 | A restore from backup brings back data whose purge date has passed | The restore rule: run the purge again, and check the due-items query returns 0, before the app serves users (RUNBOOK, "Restore after a backup") | Documented; the procedure is checked by the due-items query | RUNBOOK |
| T55 | The web app and the worker disagree on `DOCUMENT_RETENTION_DAYS`, so rows retired by one and by the other get different purge dates | Both read the same variable; the worker logs its value at startup (`worker.config document_retention_days=N`); the swap refuses a value outside 1 to 365 in SQL (ADR 0027, migration 0014) | Partly built: the log line and the SQL bound are tested (`tests/test_worker_retention_log.py`, `tests/db/test_swap.py`); the two processes sharing one value is an operator check (RUNBOOK) | RUNBOOK |
| T56 | The queue grows without bound, or a cleanup removes a job that is queued or running | `grounded.cleanup_finished_jobs` deletes succeeded jobs only, past a window of 1 to 8760 hours (checked in SQL); worker_user has EXECUTE on it and no direct delete on the queue (ADR 0028, migration 0015) | Built + tested (PR 8) | `tests/db/test_queue_cleanup.py` (only finished jobs past the window; failed, todo, doing and cancelled kept; direct delete refused) |
| T57 | The worker crashes at startup, or a periodic scheduler is given defer rights (worker_user could then enqueue any task name, reopening PR 3's hole) | The sweeps run on the worker's own timer, calling their definers; no Procrastinate periodic task is registered; the integration test runs the real worker loop as worker_user and fails on any permission error, and checks there is no EXECUTE on the periodic defer functions (ADR 0023, 0027, 0028, amended in PR 9) | Built + tested (PR 9) | `tests/db/test_worker_loop.py` (two tests, including the planted-periodic case) |

---

## Tenant Isolation: how it is enforced

Four layers. A bug in one must not be enough to leak data.

1. **Routes:** every tenant endpoint runs in a transaction that first sets `app.current_tenant` and `app.current_user` with `set_config(..., true)`. The settings end at COMMIT or ROLLBACK, so a pooled connection never carries one request's tenant into the next. The helper is `begin_tenant_transaction` (Session 3, Step 2). The FastAPI dependency uses `scope="function"`, so the commit happens before the response is sent.
2. **Policies (RLS):** every table has `ENABLE` and `FORCE ROW LEVEL SECURITY`. `USING` and `WITH CHECK` compare each row with the session tenant. No tenant set means no rows (fail closed).
3. **Grants:** `app_user` and `worker_user` have column-level grants. Nothing grants access to the password hash or the token tables.
4. **Tests:** the catalog test fails when a policy, grant, role attribute or function changes without a reviewed change to the expected lists.

### Extraction child environment (Step 7, PR 3)

The extraction child (ADR 0020) is a separate Python process, and it parses untrusted documents. Before PR 3, it inherited the worker's whole environment. That environment holds `WORKER_DATABASE_URL`, with the worker's password, and the settings of the other processes. The child now starts with an allow-list only: `PATH`, `SYSTEMROOT`, `TEMP` and `TMP` (`grounded_answers/extract/sandbox.py`, `child_environment`). It also runs with `python -I`, which ignores the `PYTHON*` settings. Tests (`tests/test_child_environment.py`) plant fake credentials in the parent, start a real child through the production launcher, and fail if the child can read any `WORKER_`, `AUTH_`, `DATABASE`, `MIGRATION_`, `APP_`, `TEST_`, `PSQL_`, `REDIS` or `VALKEY` name, or the fake values.

### Definer functions: the RLS exceptions (Step 7)

A `SECURITY DEFINER` function runs as its owner, and so it can bypass the tenant check that RLS applies to the caller. Each one is an exception to layer 2 above, so each is listed here, with the rules it must follow:

- pinned `search_path`, and every name schema-qualified;
- no EXECUTE for PUBLIC;
- a `NOLOGIN` owner role, which no one can log in as;
- validated arguments, raising on a bad value;
- a test in `test_catalog` and a test of its own behaviour, including the cross-tenant refusal;
- its owner listed in the transition guard with only the transitions it needs (ADR 0026).

| Function | Owner | Returns or changes | Status | ADR |
|---|---|---|---|---|
| `retention_due_tenant_ids()` | `retention_definer` | Returns tenant ids only, for due purges | Built (0003) | 0007 |
| `enqueue_stalled_ingest(lease_seconds)` | `retry_definer` | Defers one ingest job per version whose lease has expired. Queue rows only, with a fixed task name. A duplicate is skipped per version. It is the only way worker_user enqueues | Built + tested (PR 3) | 0023 (amended) |
| `cleanup_finished_jobs(retention_hours)` | `queue_cleanup_definer` | Deletes succeeded queue jobs older than the window (1 to 8760 hours). Owner holds the queue rights the delete and its trigger need; worker_user may execute it and nothing else on the queue | Built + tested (PR 8) | 0028 (amended) |
| `enqueue_purge_jobs()` | `purge_definer` | Defers one purge job per due item (a deleted document past its purge date, or a retired version of a live document past its date), with a queueing lock per item. Returns counts. Read policies return ids and timestamps only. worker_user may execute it and nothing else | Built + tested (PR 7) | 0027 |
| `swap_current_version(document_id, version_id, retention_days)` | `swap_definer` | Retention is the argument (1 to 365), no longer a constant. Locks the live document and makes a newer ready version current (retiring the old one), or retires a superseded one. Refuses another document or tenant, a deleted document, and a version that is not ready. worker_user may execute it and nothing else | Built + tested (PR 6) | 0024 |

---

## Authentication (ADR 0004): status

Designed, not built. The app has no login, refresh or logout endpoints yet. The design covers: EdDSA access tokens (15 min) with a pinned algorithm; opaque refresh tokens (14 days) stored hashed, rotated per use, with family revocation on reuse; per-request tenant and membership checks from the database, never from claims; login lockout and rate limits keyed on (email, IP); generic errors; an audit trail that holds no email or password.

---

## Secret scanning (gitleaks, CI `security` job)

- An inline `gitleaks:allow` comment exempts one line from the scanner. It is permitted only with a written
  reason on the same line, and every one is reviewed in the pull request that adds it. A marker never covers
  a real secret: a real secret the scanner finds is removed, and the credential is rotated.
- Reviewed exemptions:
  - `backend/src/grounded_answers/auth_keys.py` (PR #18): a type annotation on the signing-key field. The
    generic rule read the annotation's type name as a secret value. The line holds no key material, and its
    reason is written on the line.

## Secrets and credentials

| Credential | Where it lives | Who uses it |
|---|---|---|
| Superuser (`postgres`) | Root `.env` (dev), hidden prompt or CI env (tests) | Bootstrap, test fixture, operator only |
| `migration_role` | Shell only (hidden prompt, one session). Never a file. | Migrations |
| `app_user` | `backend/.env` (`DATABASE_URL`) | The web app |
| `worker_user` | The worker process's environment, as `WORKER_DATABASE_URL`. Never a file (ADR 0017). | Background jobs (1b-i: the worker runs; the purge job comes later) |
| Test cluster admin | Root `.env` (`TEST_POSTGRES_*`), passed in the shell | DB tests only, port 5433 |
| Verifiers (SCRAM strings) | Environment, for one `psql` run; redacted from error output | Bootstrap and tests |
| Answer-model API keys (ADR 0031: `GROQ_API_KEY`, `GEMINI_API_KEY` exist, free tiers, fictional data only; `ANTHROPIC_API_KEY` is for a client's own paid key, BYOK) | `backend/.env` only (git-ignored), read as `SecretStr`, sent only in a request header (never a URL or body). Never in code, logs, error messages or `repr` (tests: `test_answering_secrets_and_prompt.py`). One key per provider, created by the operator. | The web app's answer step; the evaluation runs |

Passwords are never printed. Driver error messages are not logged, because they can contain hosts.

---

## Data retention and backups (ADR 0007)

- **Live data:** a deleted document is retired at once and purged after `DOCUMENT_RETENTION_DAYS` (default 30, range 1–365). Retrieval excludes it from the moment the delete commits. *(Purge job not built; Session 3.)*
- **Backups:** purge removes live data only. Backups keep deleted content until the backup retention window expires. **The window is set by the hosting provider, which has not been chosen.** The 30-day figure does not apply to backups.
- **Audit:** `document.delete` and `document.purged` entries hold no title, filename, hash or content.

**Actions:**
- [ ] When a host is chosen, record its backup retention window here and in RUNBOOK.
- [ ] Before a restore is allowed: re-run the purge for every document whose `purge_after` is in the past. A restored backup brings back deleted content until this runs. *(Proposed; to be written into RUNBOOK with the purge job.)*

---

## Residual risks (accepted for v1, with owners)

| # | Risk | Source | Mitigation now | Next step |
|---|---|---|---|---|
| R1 | An admin can create an account for any name at an approved domain (email squatting) | ADR 0004 | Domain allowlist controlled by the operator | Email verification (v2) |
| R2 | An admin can probe which emails exist (the global unique email is an oracle) | ADR 0004 | Generic error; each attempt audited | Rate limit on the create endpoint (with the endpoint) |
| R3 | Deleted content stays in backups after purge | ADR 0007 | Stated here and in RUNBOOK | Record the backup window when a host is chosen; restore procedure (above) |
| R4 | No legal hold: a document under hold cannot outlive its purge date | ADR 0007 | None in v1 | New ADR if needed |
| R5 | A compromised worker can delete stored files | ADR 0007, ADR 0018 | No storage credentials exist: bytes are in PostgreSQL. The worker has no identity or token access, and can DELETE only `purging` bytes in its own tenant (RLS, migration 0007). | Accepted for v1 (ADR 0018). A compromised worker can remove purging bytes of its tenant. It cannot read `ready` bytes, and it cannot change them. |
| R6 | `/ready` is public and reveals which dependency is down | ADR 0009 | Names only; no hosts, versions or messages | Accepted |
| R7 | No second reviewer. Required approvals are 0, so one maintainer can merge a PR after its checks pass. A compromised maintainer account could push code that passes CI. | Process | Four required checks; admins enforced; no force pushes | Add a second reviewer and set required approvals back to 1 when a second maintainer joins |
| R8 | Pooled-connection leak test and `lazy="raise"` test not written | ADR 0005 | **Closed in Session 3, Step 2:** both tests are written and passing (T17, T18). | None |
| R11 | Timing on known users: a failed login for a known user writes an audit row, an unknown email does not. | ADR 0014 | Closed: every login response waits for a 500 ms floor, set above the measured worst case (299 ms) with margin. | Re-measure when hardware or Argon2 parameters change (ADR 0014 script). |
| R12 | Keyed client IP uses the direct address. Behind a proxy, audit rows would show the proxy. | ADR 0012 | None needed while the API is not behind a proxy | Trusted `X-Forwarded-For` handling with hosting |
| R13 | Grace window: a replayed token inside 10 seconds of its rotation gets 409 and does not revoke the family. | ADR 0004, ADR 0013 | Short window; the newest token is the only one that works | Accepted |
| R14 | Refresh and login are not rate limited per IP. | ADR 0014 | Closed: login limits per email and IP and per IP; refresh limited to 300 per IP per 10 minutes. | None |
| R15 | A Valkey flush or restart drops every lock and counter, hard locks included. | ADR 0014 | Hosting must not flush Valkey casually. The limits rebuild on the next failures. | Revisit with hosting |
| R16 | Deliberate lockout: an attacker who knows an email can lock it for 24 hours using 50 failures. | ADR 0014 | The lock is bounded to 24 h; the operator unlocks at once with `unlock-account` (audited). | Accepted |
| R9 | Rowcount checks in app write paths are not built | PROGRESS carry-over | `expect_rowcount` helper built and tested (raises and logs a line with no data); the admin CLI's domain removal refuses a 0-row delete (ADR 0010); worker transition tests assert 0-row behaviour | Use `expect_rowcount` in every app UPDATE and DELETE as they are added (Session 3, Step 3 onward) |
| R10 | Operator actions are not attributed to a person. Audit rows from the admin CLI have `actor_user_id = NULL`. | ADR 0010 | One operator; detail holds no personal data | Record the operator's identity when a second operator is added (new ADR) |
| R17 | Cross-tenant job rows. The queue has no RLS (ADR 0015). `worker_user` reads every job row. `app_user` reads every job id across tenants (the enqueue grant, ADR 0016). The ids reveal overall job volume, not content. | ADR 0015, ADR 0016 | Queue in its own schema (`queue`); grants are the reviewed list in `expected_queue_grants.json`; PUBLIC has none; `app_user` cannot read `args` (test). Argument format is checked by a test in Step 1b. | **Accepted by the owner (ADR 0016).** Argument-format test (Step 1b). |
| R18 | Queue load on the database. Polling and job writes share the database with tenant traffic. | ADR 0015 | None yet | Measure in Session 4; move the queue to its own database if needed (new ADR) |
| R19 | On Windows (development only) the extraction child has a timeout but no memory limit: the stdlib has no per-process memory limit there, and a Job Object is not added as a dependency. | ADR 0020 | Timeout and output cap on Windows; memory limit on Linux (production and CI) | Accepted for development. Production runs on Linux, where the 1 GiB limit applies and is tested in CI. |
| R21 | Document text leaves our system on every answer: the question and about five chunks go to a third-party model provider (ADR 0031). | ADR 0031 | Demo tenant only: fictional data on Groq's free tier. Real client data only with a provider that meets all five points of ADR 0031, decision 3 (no training, retention of 30 days or less, signed DPA and region, commercial use, per-tenant client approval). Prompts hold only the question and chunk text (tested). Free tiers never receive real data. | Re-read each provider's terms before the first real client; they change. Groq stays demo-only until its DPA confirms no training. |
| R22 | A model can cite a source it was not given, quote text the source lacks, state a number the source does not say, or follow an instruction hidden in a document. | ADR 0032 | Every citation is checked against the sources before an answer is shown (ref in range, quote found after normalisation, numbers of the answer present in a quote). Invalid output gets one corrective retry and then a fixed refusal. Refusal wording is ours, never the model's. Document text is framed as data in `<source>` blocks, with the tags escaped. Logs hold codes and counts only. | Residual: a valid quote does not prove the whole answer is supported (Step 7 measures groundedness); prompt injection is tested in Step 6. |
| R23 | `POST /ask` spends money on every call and sends tenant text to a third party: abuse can drain a budget or a free-tier quota, and a tenant with real data could be pointed at a provider whose terms allow training. The metering table could become a second copy of what employees ask. | ADR 0033 | Per-user and per-tenant rate limits and an in-flight limit (Valkey, fail closed); a per-tenant daily cost cap summed from `answer_usage` (fail closed). Providers per tenant in `tenant_settings`, written only by the operator; a provider is usable for `real` data only if it is in `APPROVED_FOR_REAL_DATA`, checked when the operator sets it and again before every call. Every settings change is audited with old and new values. `answer_usage` has no text and no user id by schema (CHECK lists, tested), no `UPDATE` or `DELETE` grant, RLS forced. Logs carry a keyed hash of the tenant id, never raw tenant, user or document ids. | Residual: the cap can overshoot by the requests in flight; Groq's per-minute token limit is shared by all tenants and only the global cap of Session 9 protects it; metering rows grow without a retention job (v1.1); the unparsable-reply tokens are not counted. |
| R24 | Embedding a question is CPU and memory work in the web process: a burst of questions could starve the event loop or exhaust memory, and a failed or slow model load could leave the app accepting questions it cannot answer. | ADR 0033 | The model is loaded in the background and `/ready` fails until it is loaded or if the load fails; embeddings run in threads behind a semaphore (`EMBED_CONCURRENCY`, default 2) with a 2 s slot wait and a 3 s embedding timeout, so a request cannot hold a slot or raise the thread count by giving up; the load error is logged as a class name only (no path, no message); the endpoint is off by default (`ASK_ENABLED`). | Residual: the process needs about 1.1 GB at load; one worker per container is assumed; behaviour under real concurrent traffic (database and provider calls beside embedding) is not yet measured. |
| R20 | A worker killed hard leaves a version in `processing` with no job to retry it, until its lease expires and a new job is enqueued. The periodic stalled-job retry is not built yet. | ADR 0022, D5 | The lease expires, so the version is never locked for good; a manual re-enqueue recovers it | Built in PR 3 (ADR 0023; the timer in PR 9). A timer in the worker calls the security-definer function `enqueue_stalled_ingest`, which defers one ingest job per stalled version. Residual: a killed version waits for its lease to expire (20 minutes by default) before it is retried. |

---

## Controls summary

- **Tenant isolation:** RLS with FORCE, fail-closed policies, column grants, tests (see table above)
- **Secrets:** environment variables only; `.env` git-ignored; gitleaks in CI; SCRAM verifiers; test cluster separated by port and guard
- **Input validation:** Pydantic `Settings` (unknown keys refused); password policy for database roles; account password policy (12 to 256 characters, common-password list, ADR 0010); admin CLI input checks before any database call (slug, domain, email, display name); file uploads size and type limited (Session 3)
- **Rate limiting:** login and refresh limits in Valkey, atomic, HMAC'd keys, timing floor on login (ADR 0014); built and tested
- **Audit logging:** `audit_log` table with an append-only grant for the app; the login and admin events are not yet written
- **CORS:** allowlist of exact origins, no wildcard, credentials only for listed origins
- **Dependencies:** SHA-pinned GitHub Actions; Dependabot with a 7-day cooldown; `pip-audit` and `npm audit` in CI
- **Health:** `/health` (liveness) and `/ready` (readiness, ADR 0009)

---

## Checklist Before Production

- [x] Threat model written (Session 2)
- [x] Tenant isolation tests passing (RLS verified, 90 DB tests)
- [x] Pooled-connection leak test and `lazy="raise"` test (R8, Session 3 Step 2)
- [ ] Authentication endpoints built and tested (ADR 0004)
- [ ] Braces vulnerability patched or replaced
- [x] Rate limiting built and tested (ADR 0014); deployment settings pending hosting (R15)
- [ ] Audit logging tested end-to-end
- [x] Branch protection on `main` in effect (T24; verified 2026-10-22). Required approvals 0 by choice for a solo maintainer (R7)
- [ ] Backup retention window recorded and restore procedure written (R3)
- [ ] Database storage limit checked and recorded. File bytes live in the database (ADR 0018), so its size grows with every stored version.
- [ ] Backup size and window recorded. Backups hold the bytes, not only the rows (ADR 0018, R3).
- [ ] Host disk encryption at rest confirmed and recorded. The application does not encrypt the bytes (ADR 0018).
- [ ] TLS/HTTPS enforced
- [ ] Secrets manager (AWS Secrets, Vault, or similar) deployed
- [ ] WAF (Web Application Firewall) configured
- [ ] DDoS protection enabled
- [ ] Incident response plan written

## Worker scheduling (PR 9)

The sweeps run on an in-process timer, not on Procrastinate's periodic tasks. The periodic deferrer runs as the worker's own role and defers jobs with any task name, so it needs rights worker_user must not have. Keeping it out means the worker's rights stay as they were, and the defer path stays with the definers. Two tests hold the line: `tests/db/test_worker_loop.py` runs the real worker loop as worker_user and fails on any permission error, and checks that worker_user has no EXECUTE on any `procrastinate_defer_periodic%` function. A planted periodic registration fails the first test (PR 9's planted failure).

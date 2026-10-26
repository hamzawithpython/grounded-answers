# Progress — Grounded Answers

**Last updated:** 2026-10-26 (Session 5: Steps 1 and 2 merged, Step 3 ADR 0031 written)

## Session 5 status (search and answers with citations)

Plan approved 2026-10-26. Each step has its own branch and PR, and stops for review.

| Step | Work | State |
|---|---|---|
| 1 | Retrieval evaluation harness against the test DB (`grounded_answers.evaluation`, `pytest -m eval -s`; smoke test in `-m db`) | ✅ Merged (PR #52). Results in `docs/EVAL.md` |
| 2 | Retrieval strategy: ADR 0030 (decisions approved 2026-10-26), migration `0016_drop_hnsw_index`, `RETRIEVAL_TOP_K`, `retrieve()`, the k sweep in the harness, the scale test (`-m eval`, reproducible evidence) | ✅ Merged (PR #53); dev DB at 0016 |
| 3a | Decision: answer model and provider (ADR 0031, decisions approved 2026-10-26) | ✅ Merged (PR #54) |
| 3b | Provider test: `AnswerModel` interface, Groq/Gemini/Anthropic adapters, key handling, live run on the 50 questions | ✅ Merged (PR #55). Groq works (50 of 50). `gemini-2.5-flash-lite` is closed to new accounts (404), so Gemini 3.5 Flash-Lite replaces it (5 of 5, smoke test). Haiku offline-tested only (no paid account). Keys rotated 2026-10-26. |
| 4 | Answer contract, citation validation, refusal (ADR 0032, decisions approved 2026-10-26) | ✅ Merged (PR #56). 5-question Groq check passed (no retries). |
| 5a | Metering and tenant settings (ADR 0033, decisions approved 2026-10-26): migration `0017_answer_usage_settings` (`answer_usage`, `tenant_settings`, RLS, three grants), `answering/policy.py`, `answering/usage.py`, admin commands `show-tenant-settings` and `set-tenant-settings` (audited) | ✅ Merged (PR #58). Dev DB at 0017; demo tenant set (fictional, groq, $0.05 a day). |
| 5b | Query-embedding wiring in the web process: `ASK_ENABLED`, background load, `/ready` 503 `embedder`, bounded concurrency, latency and memory measured (default `EMBED_CONCURRENCY` 2; evidence in `docs/EVAL.md`) | ✅ Merged (PR #59) |
| 5c-1 | `/ask` service without HTTP (plan approved 2026-10-26, ADR 0033): the flow with fake interfaces, the Valkey rate limit, in-flight set and metering flag, the tenant-id log helper, the AST log scan, the sentinel test (service level), the deadline in `answer_question` | Built on `feat/step-5c1-ask-service`, awaiting review |
| 5c-2 | `/ask` over HTTP: `authenticated_principal` (and its parity test with `AuthenticatedRequest`), the database stores (settings and spend, retrieval, metering), the endpoint, the lifespan wiring, the DB and sentinel tests through HTTP, a live check of at most 5 Groq questions with the metering rows shown | Planned |
| 6 | Prompt-injection tests | Planned |
| 7 | Answer evaluation (compare `RETRIEVAL_TOP_K` 5 and 8; cross-document answers may need more context) | Planned |

**Step 5 decisions (ADR 0033):** any authenticated user, question only (1 to 1,000 characters); 200 for answers and refusals, 429 for limits and the cap, 503 for service errors; limits 5 per user and 20 per tenant per minute, in-flight limit per tenant a setting (default 2); daily cost cap per tenant in micro-USD from the metering table (demo tenant $0.05); counts and codes only about questions (no text, no user id); per-tenant provider list and data class, set only by the operator, checked at set time and at call time; 12 s deadline with stage budgets; query embedding loaded in the background; logs carry `request_id` and a keyed hash of the tenant id, never raw tenant, user or document ids (project-wide rule).

**Notes from the Step 5 review (2026-10-26):**
- `answer_usage` rows grow forever. A retention or rollup job is v1.1 work, and analytics (v1.1) will want a daily rollup.
- Groq's per-minute token limit (8,000) is global across tenants, so the per-tenant cap does not protect it. The global daily cap in Session 9 does.
- Alembic's version column holds 32 characters, so migration ids must be 32 characters or fewer (`0017_answer_usage_settings` is 26).

**Step 3 decisions (ADR 0031):** demo tenant on Groq `openai/gpt-oss-120b`, free tier, fictional data only, reasoning effort `low`; candidates to test are Groq gpt-oss-120b, Gemini 2.5 Flash-Lite (free, fictional data only) and Claude Haiku 5.5 (paid reference, credit added by the operator); real client data needs all five points of ADR 0031 decision 3; local-only mode is a recorded future option; Groq is demo-only until its DPA confirms no training. The interface forbids user, tenant and email values in prompts (tested) and reports tokens, cost and latency per answer (in the eval report only in Step 3b; the per-tenant metering table with RLS, grants and catalog guard comes in Step 5 with `/ask` and the cost cap). Keys: `SecretStr`, `backend/.env` only, with a log-redaction test.

**Step 4 decisions (ADR 0032):** four-field contract (`refused`, `refusal_reason` code, `answer`, `citations` of `ref` and `quote`), no confidence field (SPEC 6.2 and 6.3 amended; the review-queue signal is `retried`, invalid-output codes and `could_not_verify`); any invalid citation invalidates the whole answer; text quotes at least 20 normalised characters, table quotes at least 8 with a word (row label and value); every number in the answer must appear in a cited quote; clause found by position inside the chunk (own-section labels, cross-references excluded), section when none, table chunks section-level; one corrective retry then `could_not_verify`; refusal wording is ours; provider failures are errors, not refusals; `<source id="n">` framing with escaped tags; English answers in v1. Built: `grounded_answers.answering` (`schema`, `validate`, `normalise`, `clauses`, `respond`, `messages`, `prompt`).

**For Step 7 (added by Step 4):** quote validation proves that the cited text exists, not that the whole answer is supported. Measure answer groundedness, the false-refusal rate of the strict citation rule and of the numeric check (paraphrases, derived numbers, numbers in words), and clause accuracy per quote (the labels are findable: 45 of 45; whether each quote gets the right one is not yet measured). Still open from Step 2: whether table chunks should carry their caption label (q012).

**For Step 7 (answer evaluation):** investigate why gpt-oss-120b refused q043 and q044 (both version questions). q004 and q022 were refused too, and are known retrieval misses at k=5 (ADR 0030). Compare `RETRIEVAL_TOP_K` 5 and 8. Ask before any full 50-question provider re-run: the Groq free tier has about 200,000 tokens per day and one run uses about 78,000.

**Step 3b rules from review (2026-10-26):** outputs of eval runs go in the git-ignored `backend/.eval-output/`, not `%TEMP%`. Direct dependencies are reviewed by exact name: `docs/DEPENDENCIES.md` plus `tests/test_dependencies_documented.py` (the scaffold's `httpx2` was replaced by the real `httpx`).
**Step 2 decisions (ADR 0030):** hybrid RRF with the standard constant 60 (c=10 gained one pair, which is noise); no reranker; no vector index (migration 0016), so vector search is exact over the tenant's rows; `RETRIEVAL_TOP_K` default 5; query embedding is loaded in the app lifespan, run with `asyncio.to_thread` bounded to about 4, and `/ready` fails until it is loaded.

**Revisit the missing vector index** when retrieval p95 passes about 250 ms or one tenant passes about 10,000 chunks (partitioning by tenant, or an index per tenant). Never add a plain HNSW index back (RUNBOOK, "Retrieval").

**Dev actions after the Step 2 PR is merged (the operator runs them):** `alembic upgrade head` on the dev database (it is at `0015_queue_cleanup`; 0016 drops the vector index). `/ready` returns 503 `schema` until then.

**Open before Step 7 (decide, then fix in ingestion):** table chunks carry only the section label (for example `6`), not the clause of the table's caption (`6.2`). So a citation of a table is section-level, and q012 counts as a miss although its top chunk is the right table. The options are to carry the caption label on the table chunk, or to keep citations of tables at section level. It changes chunking (ADR 0021), so it needs your decision.

**Not built in Step 2, decided:** the web process does not load the embedding model yet. It comes with `POST /ask` (Step 5): lifespan load before ready, `to_thread`, a limit of about 4 at a time, one worker per container (about 0.76 GB each, 1.1 GB peak while loading).

**Step 4 instruction:** answerable and unanswerable vector scores overlap (0.82 to 0.84), so refusal cannot rest on a score threshold alone. It needs model judgement plus citation validation.

**Known evidence limits (ADR 0030):** 45 pairs, so one pair is 0.022; the hybrid lead over vector is a hypothesis, and the old scratch figure of 0.76 is unexplained; 180 vs 190 chunks against the scratch set is unexplained; the scale data is synthetic copies of the real chunks.

## Session 6+ outline (revised 2026-10-26 after review; the order and the SPEC scope changes are approved, each session's decisions are still to be made one by one)

Goal: ship a **demoable v1** first. Order: Session 5 finish (Steps 5 to 7), then Sessions 6 to 9. Everything else is in "v1.1" and "v2" below. The SPEC was amended the same day with your approval (see the note at the top of `docs/SPEC.md`): conversation context, feedback and review queue, analytics, user-management and audit UI, and eval-in-CI move to v1.1; Telegram moves to v2; "time to first token" becomes "time to first visible state" (progress states; answer text is shown only after validation, ADR 0032). Every provider, price or limit fact is checked online on the day (CLAUDE.md); none is assumed here.

**What v1 means.** An employee logs in and asks a question, gets a cited answer or a fixed refusal, and opens a citation to read the source passage. A tenant admin uploads, replaces and deletes documents and sees their status. The operator creates tenants and users with the admin CLI and runs the evaluation on demand with the CLI. Each question stands alone (no history). It runs on a real host, restarts itself, reports errors, has a public try-it account with a daily cap and a reset, and has a portfolio write-up.

**Consequences of the cut for Step 5.** No conversation or feedback tables. The metering table (tokens, cost, latency, outcome code, retry flag, per tenant, with RLS, grants and the catalog guard) still comes with `/ask` for the cost cap. Decision for the Step 5 plan: store question and answer text at all, or counts and codes only (recommended in v1: counts and codes only, which also satisfies SPEC 7.1 data minimisation and removes a retention setting).

### Session 5 (finish): Steps 5 to 7 (see the table above)
`POST /ask`, prompt-injection tests, answer evaluation. Unchanged. Step 5 is designed for about 1.1 GB per web and per worker process and a pooler-agnostic connection use; if Session 6 finds a different host constraint, the rework is recorded then.

### Session 6 - Hosting feasibility and decision (research and a throwaway trial)

**Goal.** Find out whether a managed Postgres can run our bootstrap, because that can force the provider, and the provider fixes the pooler, memory and connection budget.

**Steps.**
1. Shortlist about 4 hosts (managed Postgres plus a separate app host, an all-in-one platform, a VPS with our own `docker-compose` Postgres, a free tier). From the official pages, with the date: RAM and price per instance (at least 1.1 GB per web and per worker process, so about 2.2 GB plus Postgres on one box), Postgres version, extension list, pooler, backups and retention, region, data-processing terms, free-tier limits.
2. Check each bootstrap need, first in the documentation, then on a real trial instance: `CREATE ROLE` as `LOGIN` and `NOLOGIN` with `NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT`; setting a SCRAM-SHA-256 verifier as a password (`bootstrap_cluster.sql`); schemas `AUTHORIZATION migration_role`; objects and `SECURITY DEFINER` functions owned by the NOLOGIN definer roles (ownership transfer and `SET ROLE`); `CREATE SCHEMA extensions` and `CREATE EXTENSION vector SCHEMA extensions`; `FORCE ROW LEVEL SECURITY`; the exact-grant tests; whether the provider's admin role can run all three bootstrap files.
3. Check the pooler: transaction mode against our prepared-statement setting (ADR 0005), `LISTEN/NOTIFY` for the queue (ADR 0015 decision 6), and the connection budget (web max + worker pool + worker LISTEN + migration and operator).
4. Run the migrations and `pytest -m db` against the trial instance (throwaway database, fictional data, no secrets in the repo). Record every result in SECURITY.md (hosting checklists in RUNBOOK, ADR 0018 and ADR 0021).

**Decisions.** (a) Managed Postgres, or self-managed Postgres on a VPS if the bootstrap does not fit (the fallback that removes every constraint, at the cost of running backups and upgrades yourself). (b) Where web and worker run, and whether the frontend shares a registrable domain with the API (the refresh cookie, CORS and CSRF in Session 7 depend on it). (c) Any paid service needs an ADR with price and alternative (SPEC 7.5).

**Verified by.** A pass or fail per bootstrap statement, trial output (statements and numbers, no secrets) in the ADR, and `pytest -m db` green against the trial instance. If nothing passes, the ADR says so and picks the VPS.

### Session 7 - Frontend: login, chat, admin documents (accessibility and security headers built in)

**Goal.** The v1 screens, accessible and safe by construction, not as a later pass. May be split into 7a (login and chat) and 7b (admin documents) when planned.

**Steps.**
- Login with the access token in memory only and the refresh token in an httpOnly cookie (ADR 0012, 0013); silent refresh on load and on 401 with one refresh in flight; logout; route guards.
- Chat: a progress state while the answer is produced, then the validated answer; clickable citations that open the source passage (document, clause, quoted span); the fixed refusal messages by reason code; the "could not verify" and service-unavailable states. Model text is rendered as plain text, never HTML, and no model-controlled links.
- Admin documents page: upload with type and size feedback, status (queued, processing, ready, failed with reason), replace, delete with the retention explanation. The API stays the authority; the UI hides what the API refuses.
- Accessibility from the first component: keyboard use, focus management, labels, contrast, screen-reader names for citations and status changes, reduced motion.
- Security headers from the first deploy: CSP without `unsafe-inline` (nonces or hashes), `frame-ancestors`, `Referrer-Policy`, `Permissions-Policy`, `X-Content-Type-Options`; HSTS at the host.
- Frontend dependency and licence review by exact name (as for the backend).

**Decisions.** Call the API directly or through a Next.js server layer (cookies, CORS, CSRF); CSRF protection on the cookie-based refresh and logout (SameSite value, a custom header, or both); polling interval for ingestion status; CSP strictness against the framework's inline needs; accessibility level to claim (WCAG 2.2 AA is the usual target); component and chart-free styling within the existing Tailwind setup.

**Verified by.** Vitest and Testing Library tests for the auth flow (expired access token, rotated refresh, reuse detection showing logged-out); a Playwright run against a local stack (login, ask, open citation, refusal, admin upload and delete, logout); axe checks with zero serious findings; a CSP test that fails when an inline script appears and a header test; an employee-versus-admin access test on every admin route and endpoint; lint and typecheck in CI; a written manual keyboard and screen-reader pass in SECURITY.md.

### Session 8 - Deployment and observability

**Goal.** The system runs on the chosen host, restarts itself, and tells you when it is unhealthy.

**Steps.** Images for web and worker (model files baked in at build, never downloaded at start, ADR 0022); secrets in the host's secret store, with key rotation steps in the RUNBOOK; HTTPS and redirects; migrations as a release step with a rollback note; backups and the restore rule for purged documents (ADR 0007 and the RUNBOOK restore section: after a restore, re-run the purge so deleted documents do not return), with a restore drill; **worker restart policy together with the deferred bounded-reconnect change** (PROGRESS, Session 4 "Change 2"), tested with a real database restart; structured logs with no document or question text; **error tracking** with request bodies scrubbed; LLM tracing that records ids, token counts and codes only; alerts for `/ready` failing, failed-job count, queue age, cost cap reached, provider 429 or 5xx rate, p95 latency, storage growth (ADR 0018); an uptime check.

**Decisions.** Observability stack within free tiers (what each retains and where it processes data); log retention period (SPEC 7.1); alert channel; one worker per host or sharing a box (memory).

**Verified by.** A deploy from a clean checkout following only the RUNBOOK; a kill-the-worker drill and a database-restart drill with recovery; a restore drill into a scratch database; an induced alert for each alert rule; a secret scan of the repo history and the image.

### Session 9 - Public demo and portfolio

**Goal.** Anyone can try the product without hurting it, the budget, or the data; and the proof is published.

**Demo risks to design for.** Quota drain: the Groq free tier is about 200,000 tokens a day and one answer is about 1,900 tokens, so roughly 100 answers a day for everyone together, and one visitor can use it all. Prompt-injection and abuse text through the question box; scraping of the document set; posted credentials and account sharing; uploads of hostile files; the demo tenant being altered; a provider outage showing as a broken site.

**Steps.** An employee-only demo user (no upload, delete or write beyond asking); a read-only or screenshot tour for the admin page; per-account, per-IP and **global daily caps** with a clear "demo budget used today, back tomorrow" message (on top of the Step 5 per-tenant cost cap); question length limit; lower rate limits for the demo tenant; a second provider as fallback (Gemini free tier, fictional data only); the **demo tenant reset job** (restore seeded documents, clear any visitor-created rows, idempotent, audited, through a narrowly granted definer) on a schedule; a banner that the data is fictional.
Portfolio: final evaluation run on the deployed system with results in `docs/EVAL.md` against SPEC 7.2 (recall@5 0.85, groundedness 0.90, citation accuracy 0.90, correct refusal 0.90) and 7.3 (latency, cost); the second fictional tenant added to the isolation tests (SPEC §3); security write-up from SECURITY.md (threat model, controls, cross-tenant test results, dependency review, the `httpx2` near-miss); README with quick start and architecture diagram; 60-second demo video; case study.

**Decisions.** Public credentials or email-gated sign-up; the cap numbers; reset frequency; what to publish of the evaluation set and of failures (recommended: publish the failures too); repository licence; whether to seek one real pilot user (stretch).

**Verified by.** Tests for each cap (user, IP, global) and the over-budget message; a test that the demo user is refused every write the admin may make; a reset test that restores a visitor-modified tenant and leaves other tenants untouched; a staged primary-provider outage that shows the fallback or the unavailable message; every number in the README and case study traceable to a committed report; a fresh-clone run of the README quick start; a final secret scan of the full history.

### v1.1 (after the demoable v1 ships; scope change approved 2026-10-26)

Conversation context for follow-ups (stored per user, tenant-scoped, RLS, with a retention setting; history length; the prompt-privacy rule still holds); feedback (helpful or not, optional comment) and the review queue (refused, down-voted, retried, `could_not_verify`, ADR 0032); analytics (questions per day, top topics, refusal rate, helpfulness, median time, cost per answer); tenant-admin user management and the audit-log view; evaluation from the admin panel and charted, and the eval job in CI (small subset per PR with a fake or recorded provider, full set nightly or on demand, fictional data only). Each gets its own plan, ADR where a decision is involved, and cross-tenant tests for every new table.

### v2 (approved 2026-10-26)

Telegram channel (one-time account linking with a short-lived code, signed webhook, same `/ask` path and caps); SSO; Urdu answers (after English evals pass).

**Open question for review:** none that blocks Step 5. Assumption to confirm: in v1 the on-demand evaluation is run by the operator with the CLI, and the admin-panel button waits for v1.1.

## Session 3 complete (2026-10-22)

| Step | Work | State | PR |
|---|---|---|---|
| 1 | Admin CLI: tenants, domains, first admin, unlock (ADR 0010) | ✅ Merged | #16 |
| 2 | ORM models, tenant transaction helper, R8 tests, rowcount check (ADR 0011) | ✅ Merged | #17 |
| 3a | Keys, login, per-request checks (ADR 0012) | ✅ Merged | #18 |
| 3b | Refresh rotation, reuse, logout (ADR 0013) | ✅ Merged | #19 |
| 3c | Login limits in Valkey, timing floor (ADR 0014) | ✅ Merged | #20 |
| 4 | Background job runner decision: Procrastinate (ADR 0015) | ✅ Merged (`0e70fa9`) | #21 |

Open from Session 3: the `icacls` and HMAC key steps on your machine (RUNBOOK, Auth keys), and the Step 8 docs
from Session 2 (SECURITY, RUNBOOK, LEARNING_LOG, CHANGELOG). The R9 rowcount check is in place for the first app
UPDATE or DELETE.

## Session 4 status

Steps are split as you decided. Each step has its own branch and PR, and stops for review.

| Step | Work | State |
|---|---|---|
| 1a | Migration `0005` (Procrastinate 3.10.0 in schema `queue`), grants, catalog guard, connector and enqueue tests (ADR 0016) | ✅ Merged (PR #23). 119 unit and 182 DB tests passed at review. |
| 1b-i | `WorkerSettings` (env only), worker entry point as `worker_user`, `begin_worker_transaction`, redacting log filter, migration `0006` (prune grant) (ADR 0017) | ✅ Merged (PR #25). |
| 1b-ii | Enqueue on the tenant transaction (outbox), UUID-only arguments, task allowlist (empty until ingestion), web app log redaction, rollback guarantee, poison-job rule with a test-only task (ADR 0017 amendment) | ✅ Merged (PR #26). Unit 145, DB 198 passed at review. Planted separate-connection enqueue made the rollback test fail, then removed. |
| 2a | File storage decided (ADR 0018: bytes in PostgreSQL, B2 as the scale-up path). Migration 0007: `document_blobs`, grants, policies, checks, model, tests | ✅ Merged (PR #27). |
| 2b | `ObjectStore`, `PostgresObjectStore` (caller's tenant transaction), in-memory fake, contract test, rowcount allowed set (ADR 0018, amendment 2) | ✅ Merged (PR #28). |
| 3 | Upload endpoint `POST /documents` (ADR 0019): tenant_admin, streaming 10 MiB cap, type from bytes, sanitised filename, duplicates (live only), one transaction with the outbox job, rate limits. Off until Step 6 (`UPLOAD_ENABLED=false`). | ✅ Merged (PR #29). |
| 4 | Text extraction (ADR 0020): pypdf, python-docx, markdown-it-py in a child process with timeouts and a Linux memory limit; DOCX XXE and billion-laughs tested; no-text PDFs refused (SPEC §6.1 limitation, awaiting approval of the wording) | ✅ Merged (PR #30). Linux container run: 45 passed, demo set under 1 GiB. |
| 5 | Embedding model and chunking (ADR 0021, `docs/EVAL.md`): multilingual-e5-small, S1 chunks. Chunks table: extensions schema and pgvector (bootstrap), migration `0008_chunks` | ✅ Merged (PR #32 docs, PR #33 chunks table). Retrieval strategy is Session 5. |
| 6 | Ingestion worker (ADR 0022): lease and fencing token, one-transaction write, retries, `embedding` failure reason (amends ADR 0020), pinned model files with hash checks, migration `0009_ingestion_lease`, CI model cache | ✅ Merged (PR #34). Upload was off until Step 8. |
| 7 | Replace, delete, purge and the retry that closes R20 (ADR 0023 to 0029). Decisions approved 2026-10-24. Build order: PR 1 docs (ADRs, merged as #39); PR 2 per-role transition guard and app_user grant revoke (migration 0010, merged as #40); PR 3 stalled-version retry (migration 0011, definer `enqueue_stalled_ingest`; ADR 0023 amended), with the child-environment fix and the bootstrap split (merged as #41); PR 4 delete (migration 0012 restore guard; `DELETE /documents/{id}`; ADR 0025 amended; merged as #42); PR 5 document list (`GET /documents`, ADR 0029 amended; merged as #43); PR 6 replace and swap (merged as #44; migration 0013); PR 7 purge (merged as #45; migration 0014); PR 8 queue cleanup (definer `cleanup_finished_jobs`, daily; ADR 0028 amended; merged as #46); PR 9 is the sweep timer (merged as #50). | ✅ Merged (PRs #39 to #46 and #50). |

| 8 | End-to-end with `demo-data/`, `UPLOAD_ENABLED` on | ✅ Live run on the dev machine, results below. Runbook fixes in PR #49 (awaiting merge). |

**Session 4 is complete (2026-10-26).** Every step is merged. Step 8 ran end to end on the dev machine with the real model, the real worker and `UPLOAD_ENABLED` on. The only open PR from this session is #49 (runbook fixes), which is awaiting merge.

**Step 8 live run (recorded 2026-10-26, by the user on the dev machine):**
- 7 uploads returned 201, and all 7 became `ready` (9 to 18 s each).
- The replacement (version 2) of IFL-FIN-007 returned 201. The swap was correct: version 1 retired, version 2 current.
- Chunks per version: 23 (version 1, retired), then 25, 30, 24, 25, 18, 18, 17. Every chunk has a clause label.
- `document.ready` is 8 and `document.swap` is 8: one swap for each version that became current (runbook 8e).
- `app_user` sees only current versions. The Expense Policy shows version 2 only.

**Found in the run, and fixed:**
- The worker crashed at startup with `permission denied` for the periodic deferrer. Fixed in #50: the three sweeps run on an in-process timer, and no periodic task is registered.
- Three worker log lines were missing: `worker.config`, `purge.sweep` and `ingest.ready`. Fixed in #51. A real-process test now checks every startup line and the ingest success line.
- Runbook errors: Step 1c (run `alembic current` before removing the migration URL), 3a (BOM-free `.env`), the Step 7 and 8e swap counts, 8a (`chunks` has no `id` column; count `position`), and 8c (the password went on the docker command line). All of these are in PR #49.

**Known limits to revisit with evidence (ADR 0020):** PDF tables are text in v1 (D-A). If Session 5 PDF table questions fail, pdfplumber is the first candidate. PDF heading levels use font size only for unnumbered headings. Windows development has no memory limit (SECURITY R19).

**For Step 4 (PDF extraction): the worker needs size and time limits on PDF parsing.** A PDF can carry streams that expand far beyond their size on disk (decompression bombs). Step 4 must cap the decompressed size, the page count and the parse time, and run the parser under those limits, before any text is kept (SPEC §6.1, ADR 0019 D3). The upload only checks the magic bytes.

**Decided in 1a (ADR 0016):** `app_user` gets `SELECT (id)` on `procrastinate_jobs`, `INSERT (job_id, type, at)` on `procrastinate_events` and `USAGE` on the events sequence, so enqueue works. R17 accepted: `app_user` reads job ids across tenants (ids only). `psycopg-pool` (LGPL-3.0-only, unmodified) accepted.

**Known open issue (not Session 4):** `tests/db/test_rate_limit.py::test_hard_lock_after_fifty_failures_and_operator_release`
failed once in three full DB runs. Being fixed in a separate PR before Step 1b (see "Next").

**Step 7 facts verified in PR 1 (Procrastinate 3.10.0, from the installed source):** `get_stalled_jobs` has two modes, by start time and by worker heartbeat; neither reads the version lease, so ADR 0023 uses a state-based sweep. `queueing_lock` raises `AlreadyEnqueued` on a duplicate defer while one is waiting. `remove_old_jobs` is a built-in task (`procrastinate.builtin_tasks`). Procrastinate's web docs differ between the stable and main branches (stable uses heartbeats; main uses a 30-second `doing` rule), so the installed source is the reference. `DOCUMENT_RETENTION_DAYS` is not in code (PR 7). `app_user` already has UPDATE on `documents.current_version_id`, which no app code uses (PR 2 revokes it).

**Done since the last update (2026-10-24):** PR #38 (worker startup login check) is merged as `a25607f`. The dev cleanup is done: the stray test database is dropped, the dev role passwords are reset, and the dev database is at migration `0009`. `/ready` returns 200 and the worker smoke test passed. Step 7 is in planning (no code).

**Worker logging (branch `fix/worker-startup-logs`, PR open, not merged):** the live Step 8 run found three missing lines in the worker log: `worker.config`, `purge.sweep` and `ingest.ready`. The fix is in `jobs/logging_filter.py` (every job logger has a handler) and `jobs/worker.py` (an explicit logger name, because `python -m` makes `__name__` `__main__`). Tests: a coverage test over the worker's module chain, a real-process test that queues one ingest job and waits for the five startup lines and the ingest success line, and an in-process loop test. Planted failure (`grounded_answers.ingest` removed from the handler list) fails the real-process test; restored.

**Worker startup login check (PR #38, merged, change 1):** the worker checks the `worker_user` login once at startup, after the model loads, with a 10 s timeout. A wrong password stops it with `worker not started: database login failed for worker_user` and exit code 1. A database that is unreachable also stops it (`database check failed`). The check is startup-only.

**Change 2, deferred to the deployment session: bounded reconnect, then exit.** When the database drops while the worker runs, retry the connection a bounded number of times, then exit non-zero. Do not build it alone: without a supervisor, a brief database restart would stop the dev worker for good. It must ship with a restart policy (for example a Docker `restart: unless-stopped` or a systemd unit) and a test with a real database restart.

**Other 1a facts:** `EXPECTED_SCHEMA_HEAD` moved to `0005_procrastinate_schema`. The bootstrap (`scripts/bootstrap_database.sql`) creates schema `queue` and sets `search_path = grounded, queue` for `app_user` and `worker_user`. Existing dev databases must re-run the bootstrap (RUNBOOK) before `alembic upgrade head`.

## Session 4 plan (original, as written in Session 3)

1. **Migration 0005:** Procrastinate 3.10.0's schema, grants for `app_user` and `worker_user`, and the catalog
   guard entries (ADR 0015, decisions 1 and 2). The exact grant list comes from the function bodies. Upgrades are
   new migrations.
2. **Upload and ingestion:** upload endpoint (size and type checked from content, ADR 0007, SPEC §6.1), ingestion
   job with ids-only arguments, an idempotency test, and the tenant-mismatch test (ADR 0015, decision 9).
3. **Purge job** (ADR 0007): periodic task, idempotent steps, `DOCUMENT_RETENTION_DAYS` setting.
4. **Periodic cleanup** of finished jobs with `remove_old_jobs` (ADR 0015, decision 5).
5. **Host selection:** pooler mode (LISTEN/NOTIFY, ADR 0015 decision 6) and the connection budget (decision 7).

## Later improvements (not yet scheduled)

- **Failed-job retention (ADR 0028, decided in PR 8).** Failed queue jobs are kept indefinitely, for diagnosis, so they can grow without bound. Add a separate, longer retention window for failed jobs (30 to 90 days, to be chosen), in its own definer or as a second argument to `cleanup_finished_jobs`, with the same exact-grant tests as PR 8. Not scheduled.
- **Worker sweep timer (PR 9).** The sweeps moved from Procrastinate periodic tasks to an in-process timer after Step 8 found the worker crashing at startup. Integration test added. Merged as PR #50.
- **Separate `valkey-test` service in the test profile.** The test Valkey shares the dev Valkey container, on
  database 15, and the test fixture flushes that database. Postgres already has a separate `postgres-test`
  service on port 5433. Mirror that for Valkey, so tests never touch the dev instance's data (ADR 0006 pattern).

## Session 2 status at a glance (historical)

| Step | Work | State | Where |
|---|---|---|---|
| 1 | ESLint 9 → 10 | ✅ Merged | PR #8 |
| 2a | Auth decision (self-built JWT) | ✅ ADR 0004 Accepted, amended twice | PR #9 |
| 2b | DB access (async SQLAlchemy 2.1 + psycopg 3) | ✅ ADR 0005 Accepted | PR #10 |
| 2c | DB test strategy (service container, real logins) | ✅ ADR 0006 Accepted | PR #12 |
| 3 | Schema design (8 tables, composite FKs, ON DELETE RESTRICT) | ✅ `docs/ARCHITECTURE.md`, ADR 0007 | PR #12 |
| 4 | Roles and migrations (0001 tables, 0002 grants) | ✅ | PR #12 |
| 5 | RLS policies, definer functions, `create_tenant_user` (0003) | ✅ | PR #12, commit `b2192d0` (CI green) |
| 6 | Proof by tests | 🟡 Mostly done (see below) | working tree |
| 7 | `/ready` endpoint (redis-py 8.1.0, ADR 0008; checks and cache, ADR 0009; migration 0004) | ✅ Merged | `main` (PRs #14, #15; branch `feat/ready-endpoint` deleted) |
| 8 | Docs: SECURITY.md, RUNBOOK, LEARNING_LOG, CHANGELOG | 🟡 Partly done | — |

## Step 6: what the tests prove (DB suite: 83 passing, 0 skipped)

Run with `pytest -m db` against `grounded_answers_test`. Each run drops and rebuilds that database.

- **Bootstrap:** the cluster and database SQL files run through psql, the same as the operator's commands. Verifiers come from libpq (`PGconn.encrypt_password`). Each login role authenticates with the password whose verifier was set.
- **Migrations:** the round-trip (upgrade, downgrade, upgrade) runs in its own database, `grounded_answers_test_roundtrip`, which is dropped afterwards. It is order-independent.
- **Catalog guard:** role attributes, PUBLIC privileges, schema owner, RLS enabled and forced on every table (`alembic_version` is the one documented exemption), `SECURITY DEFINER` search_path pinning and no PUBLIC EXECUTE, and an exact grants list (`expected_grants.json`).
- **Read isolation as app_user:** no tenant context gives zero rows; a tenant sees exactly its own rows, compared to the admin's ground truth.
- **Cross-tenant INSERT:** refused on every table, including `audit_log` with a NULL actor. The test records whether the grant or RLS refused it.
- **`create_tenant_user`:** success; seven refusal causes all give the same SQLSTATE and message; no row is created on refusal. The actor and tenant come from the session settings, never from parameters.
- **Worker purge transitions:** retired → purging only; delete only purging; a document can be deleted only once deleted and with no versions left; no cross-tenant change; no access to identity or token tables.
- **`updated_at` trigger** runs for app_user, and app_user cannot write `updated_at` itself.

Unit suite: 38 passing. Both suites run in CI (`backend` with `-m "not db"`, `backend-db` with `-m db`).

## Not done yet in Session 2

- **Step 8, docs.**
  - `docs/SECURITY.md`: threat model; tenant-isolation controls; the residual risks in ADR 0004 (email squatting inside approved domains, admin enumeration, the defence-in-depth note on the actor check); backups keep deleted content after purge (ADR 0007).
  - `docs/RUNBOOK.md`: `/ready` and rollback steps; enabling branch protection (steps given in chat, not yet applied to `main`).
  - `docs/LEARNING_LOG.md`: this session's concepts and interview Q&A (RLS, `set_config` versus `SET`, `SECURITY DEFINER`, SCRAM, composite foreign keys, the silent REVOKE).
  - `CHANGELOG.md`: Session 2 entries are present; add the migrations and tests.

## Open items and decisions to remember

- **App auth code path is not built yet.** The login, refresh and logout endpoints, and the step that sets `app.current_user` and `app.current_tenant` together, come with the first endpoint that needs them. `create_tenant_user` has no caller in the app yet.
- **Branch protection on `main`** is not configured. Required checks: `backend`, `backend-db`, `frontend`, `security`. Add `backend-db` only after it has run on `main`.
- **Retention:** `DOCUMENT_RETENTION_DAYS` is in ADR 0007, but the setting is not in `Settings` yet. Add it with the purge job (Session 4).
- **Backups:** the backup retention window depends on the hosting provider. Record it in SECURITY.md when a host is chosen.
- **Managed Postgres:** when a host is chosen, check whether its pooler runs in transaction mode (set `prepare_threshold=None`) and whether it allows `CREATE ROLE` and `ALTER ... OWNER` (ADR 0005, ADR 0004).
- **Windows event loop:** the dev server must start with `--loop grounded_answers.runtime:selector_loop_factory` (RUNBOOK).
- **Dependency upgrades:** wait 7 days after a release before a manual upgrade (RUNBOOK). No date-based holds in `pyproject.toml`.

## Session 3 plan: auth and foundations

Uploads need authentication, so Session 3 builds auth first. Ingestion and the purge job move to Session 4. SPEC.md is unchanged (§6.1 still lists ingestion as v1 scope).

1. ✅ **Admin CLI** (ADR 0004, ADR 0010; built on `feat/admin-cli`, awaiting review). Commands: create-tenant, add-domain, remove-domain, list-domains, create-admin. Click in the `operator` group; argon2-cffi for the first admin's password. Tests: unit 78 passing, DB 101 passing (incl. 11 for the CLI). `create-admin` checks the tenant and domain before the password prompt.
   - Manual check on the dev DB passed (create-tenant, add-domain, list-domains, create-admin with hidden prompts, mismatch refused, duplicate slug refused, wrong domain refused).
   - Dev tenant `indus-freight` (domain `indusfreight.example`, first admin `hr.manager@indusfreight.example`) now exists in the dev database.
   - `demo-data/` (Indus Freight Lines demo documents) is the owner's work. It is not in these commits; it goes on branch `data/demo-tenant` later. No migration added: migration 0003 already covers `migration_role` writes.
2. ✅ **ORM models, tenant transaction, R8, rowcount** (ADR 0005, ADR 0011; built on `feat/orm-models`, awaiting review). Nine models mirror the migrations (names checked by Alembic's comparison). Reviewed CHECK list in `tests/db/expected_checks.json` (name and definition). `begin_tenant_transaction` and `make_tenant_session` (dependency, `scope="function"`). R8 tests: pooled-connection leak on one physical connection (commit, rollback, error), statement order, `lazy="raise"`, deferred `password_hash`. `expect_rowcount` (R9). Tests: unit 85 passing, DB 116 passing. Mutation checks: planted drift and session-scope `set_config` are both caught. The admin CLI stays on explicit SQL (decision, not an oversight).
3. **Auth endpoints** (ADR 0004): login, refresh (rotation, reuse detection, 10 s grace), logout, logout-all, EdDSA keys with `kid`, lockout and rate limits in Valkey, generic errors with a dummy hash, audit entries, per-request membership and tenant-status checks. Tests for every rule in ADR 0004. Endpoint plan and key-management steps are reviewed before code.
4. **Background job runner ADR** (for Session 4): compare Celery, arq, Dramatiq and RQ. No implementation in Session 3.

Each step has its own branch and stops for review. Nothing is committed on `main`.

## Session 3 carry-over: check rowcount on every RLS-filtered write

- An UPDATE or DELETE that RLS filters out affects **0 rows and raises no error**. Only a `WITH CHECK` violation raises (`InsufficientPrivilege`, "row-level security").
- So the purge worker and every app write path must check `rowcount`. An unexpected 0 is a failure: log it and raise an alert. It is never treated as success.
- Tests required with the first write path that depends on this: the purge worker's transitions, and each app UPDATE or DELETE. The worker transition tests already assert the 0-row behaviour (`tests/db/test_worker_policies.py`).
- **Built in Session 3, Step 2:** `expect_rowcount` (logs one line with the operation name and counts, never row data; raises `RowcountMismatchError`), tested on an RLS-filtered update. No app UPDATE or DELETE uses it yet, so every app write added from Step 3 must call it.

## Next session starts here

1. Review the Step 1a PR (`feat/job-queue-foundation`). Do not merge until you approve.
2. Separate PR: fix the flaky rate-limit test (root cause and repeated-run proof).
3. Step 1b: worker, `WorkerSettings`, enqueue helper, poison-job test.
3. Before Session 4 ends: finish the Session 2 Step 8 docs, the `icacls` step on your machine, and the LEARNING_LOG entry for Session 4.

## Completed before Session 2

### Project setup
- ✅ Monorepo layout (backend/, frontend/) — ADR 0001
- ✅ Python dependency manager: uv — ADR 0002
- ✅ Node package manager: npm — part of ADR 0002
- ✅ In-memory cache: Valkey 9.1.2 (BSD license, open-source fork) — ADR 0003
- ✅ docker-compose.yml (PostgreSQL 17 + pgvector 0.8.7, Valkey 9.1.2)

### Backend (FastAPI, Python 3.12)
- ✅ Package structure, settings (pydantic-settings), `/health`, CORS allowlist (no `"*"`)
- ✅ Quality gates: ruff, mypy strict, pytest; CI job

### Frontend (Next.js 16, React 19, TypeScript)
- ✅ Scaffold, strict TypeScript, StatusCard, Vitest tests
- ✅ Quality gates: eslint (now v10), tsc, vitest, npm audit, build; CI job

### CI/CD and security
- ✅ `.github/workflows/ci.yml`: backend, backend-db, frontend, security (gitleaks)
- ✅ `.github/dependabot.yml`: weekly, with a 7-day cooldown on uv, npm and github-actions
- ✅ `.gitleaks.toml`, `docs/SECURITY.md` (known advisories)

## Local environment notes

- **Windows PostgreSQL:** stop any local PostgreSQL 17/18 service. Docker Compose provides the database on `127.0.0.1:5432`.
- **Node.js:** 24.15 or later (`.nvmrc`).
- **Backend:** run uvicorn with `--factory` and `--loop grounded_answers.runtime:selector_loop_factory`.
- **Database tests:** need Docker running and `TEST_ADMIN_DATABASE_URL` and `PSQL_COMMAND` set (RUNBOOK, "Running Database Tests").
- **Local `.env` files** are git-ignored. `CORS_ORIGINS` and `NEXT_PUBLIC_API_URL` must use `127.0.0.1`, not `localhost` (RUNBOOK, Dev Host Rule).

## Notes

- Tests are independent. None relies on a developer's `.env`.
- CORS is an allowlist with credentials and exact origins.
- Telemetry is disabled in the frontend.
- `uv.lock` and `package-lock.json` are committed.

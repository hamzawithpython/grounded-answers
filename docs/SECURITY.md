# Security — Threat Model and Controls

**Last updated:** 2026-10-22 (Session 3, Step 1: admin CLI, ADR 0010). Status words are used exactly:
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
| T15 | Token forgery, algorithm confusion, refresh-token theft | EdDSA pinned, `kid` selects the key; refresh tokens hashed, rotated, with reuse detection and a 10 s grace window | Designed (ADR 0004) | No endpoints yet |
| T16 | Password guessing and spraying | Lockout keyed on (email, IP), per-IP rate limit, progressive delay, generic 401, dummy Argon2 hash for unknown emails | Designed (ADR 0004) | No endpoints yet |
| T17 | Tenant context leaks between pooled connections | `set_config(..., true)` as the first statement of every tenant transaction; `begin_tenant_transaction` helper | Designed (ADR 0005). **The leak test is not written.** | Required before the first tenant endpoint (ADR 0005 test list) |
| T18 | Lazy loading returns a silent partial result | `lazy="raise"` on every relationship | Designed (ADR 0005). **No ORM models or test yet.** | Required with the first model |
| T19 | Public readiness endpoint leaks internal details | 503 names only the dependency; log has exception class names only; 2 s cache limits load | Built + tested | `test_ready` (`test_failure_detail_is_logged_without_secrets`), `test_readiness_db` |
| T20 | Brute-force or resource exhaustion on `/ready` | Result cached 2 s; checks under 1 s timeout each and a 2 s budget | Built + tested | `test_ready` (`test_repeated_calls_within_ttl_do_not_recheck`, timeout tests) |
| T21 | Secrets committed to git | `.env` git-ignored; gitleaks in CI; test-only secrets marked as such | Built | CI `security` job |
| T22 | Dependency with a known vulnerability | Dependabot with a 7-day cooldown; `pip-audit` in CI; `npm audit` | Built | CI jobs `backend`, `frontend`, `security` |
| T23 | Cross-site request forgery on refresh | Refresh cookie is httpOnly, SameSite and exact-origin CORS; `Origin` checks (ADR 0004 cookie design) | Designed (ADR 0004) | No endpoints yet |
| T24 | Unauthorised change to `main` | Repo is public; `main` requires 4 status checks (`backend`, `backend-db`, `frontend`, `security`), strict up-to-date checks, admins enforced, no force pushes or deletions. Required approvals are 0 (solo maintainer; see R7). | Built (verified via the branch protection API) | RUNBOOK, "Branch protection" (the verify command) |
| T25 | Migration credential leaks through the admin CLI (argument, shell history, `.env`, or a wrong login) | Migration URL from the process environment only; hidden prompt when the URL has no password; CLI never reads `backend/.env`; passwords are never arguments; CLI refuses any login other than `migration_role`; errors show the exception class, not driver text | Built + tested | `tests/test_admin_cli.py` (`test_missing_migration_url_is_refused`, `test_invalid_url_message_does_not_echo_the_url`, `test_url_without_password_prompts_for_it`), `tests/db/test_admin_cli.py` (`test_cli_refuses_to_run_as_app_user`) |
| T26 | Weak first-admin password, or a password hash that leaks or is weak | Policy: 12 to 256 characters, not on the 10k common-password list, typed twice; Argon2id with the pinned defaults (ADR 0010); hash stored, plaintext never stored or logged; `app_user` cannot read `password_hash` | Built + tested | `tests/test_passwords.py` (policy and pinned Argon2 parameters), `tests/db/test_admin_cli.py` (`test_create_admin_writes_user_membership_and_audit`, `test_app_user_cannot_read_the_password_hash`) |

---

## Tenant Isolation: how it is enforced

Four layers. A bug in one must not be enough to leak data.

1. **Routes:** every tenant endpoint runs in a transaction that first sets `app.current_tenant` with `set_config(..., true)`. The setting ends at COMMIT or ROLLBACK, so a pooled connection never carries one request's tenant into the next. *(Designed; the helper is not written yet.)*
2. **Policies (RLS):** every table has `ENABLE` and `FORCE ROW LEVEL SECURITY`. `USING` and `WITH CHECK` compare each row with the session tenant. No tenant set means no rows (fail closed).
3. **Grants:** `app_user` and `worker_user` have column-level grants. Nothing grants access to the password hash or the token tables.
4. **Tests:** the catalog test fails when a policy, grant, role attribute or function changes without a reviewed change to the expected lists.

---

## Authentication (ADR 0004): status

Designed, not built. The app has no login, refresh or logout endpoints yet. The design covers: EdDSA access tokens (15 min) with a pinned algorithm; opaque refresh tokens (14 days) stored hashed, rotated per use, with family revocation on reuse; per-request tenant and membership checks from the database, never from claims; login lockout and rate limits keyed on (email, IP); generic errors; an audit trail that holds no email or password.

---

## Secrets and credentials

| Credential | Where it lives | Who uses it |
|---|---|---|
| Superuser (`postgres`) | Root `.env` (dev), hidden prompt or CI env (tests) | Bootstrap, test fixture, operator only |
| `migration_role` | Shell only (hidden prompt, one session). Never a file. | Migrations |
| `app_user` | `backend/.env` (`DATABASE_URL`) | The web app |
| `worker_user` | Worker environment (not built yet) | Purge job |
| Test cluster admin | Root `.env` (`TEST_POSTGRES_*`), passed in the shell | DB tests only, port 5433 |
| Verifiers (SCRAM strings) | Environment, for one `psql` run; redacted from error output | Bootstrap and tests |

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
| R5 | A compromised worker can delete stored files | ADR 0007 | Worker has no identity or token access | Storage credentials scoped to `tenant/` prefixes |
| R6 | `/ready` is public and reveals which dependency is down | ADR 0009 | Names only; no hosts, versions or messages | Accepted |
| R7 | No second reviewer. Required approvals are 0, so one maintainer can merge a PR after its checks pass. A compromised maintainer account could push code that passes CI. | Process | Four required checks; admins enforced; no force pushes | Add a second reviewer and set required approvals back to 1 when a second maintainer joins |
| R8 | Pooled-connection leak test and `lazy="raise"` test not written | ADR 0005 | Designed controls; no tenant endpoints exist yet | Write both with the first tenant endpoint and the first model |
| R9 | Rowcount checks in app write paths are not built | PROGRESS carry-over | Worker transition tests assert 0-row behaviour; the admin CLI's domain removal refuses a 0-row delete (ADR 0010) | Build with the first app UPDATE or DELETE (Session 3) |
| R10 | Operator actions are not attributed to a person. Audit rows from the admin CLI have `actor_user_id = NULL`. | ADR 0010 | One operator; detail holds no personal data | Record the operator's identity when a second operator is added (new ADR) |

---

## Controls summary

- **Tenant isolation:** RLS with FORCE, fail-closed policies, column grants, tests (see table above)
- **Secrets:** environment variables only; `.env` git-ignored; gitleaks in CI; SCRAM verifiers; test cluster separated by port and guard
- **Input validation:** Pydantic `Settings` (unknown keys refused); password policy for database roles; account password policy (12 to 256 characters, common-password list, ADR 0010); admin CLI input checks before any database call (slug, domain, email, display name); file uploads size and type limited (Session 3)
- **Rate limiting:** designed (ADR 0004); not built
- **Audit logging:** `audit_log` table with an append-only grant for the app; the login and admin events are not yet written
- **CORS:** allowlist of exact origins, no wildcard, credentials only for listed origins
- **Dependencies:** SHA-pinned GitHub Actions; Dependabot with a 7-day cooldown; `pip-audit` and `npm audit` in CI
- **Health:** `/health` (liveness) and `/ready` (readiness, ADR 0009)

---

## Checklist Before Production

- [x] Threat model written (Session 2)
- [x] Tenant isolation tests passing (RLS verified, 90 DB tests)
- [ ] Pooled-connection leak test and `lazy="raise"` test (R8)
- [ ] Authentication endpoints built and tested (ADR 0004)
- [ ] Braces vulnerability patched or replaced
- [ ] Rate limiting deployed
- [ ] Audit logging tested end-to-end
- [x] Branch protection on `main` in effect (T24; verified 2026-10-22). Required approvals 0 by choice for a solo maintainer (R7)
- [ ] Backup retention window recorded and restore procedure written (R3)
- [ ] TLS/HTTPS enforced
- [ ] Secrets manager (AWS Secrets, Vault, or similar) deployed
- [ ] WAF (Web Application Firewall) configured
- [ ] DDoS protection enabled
- [ ] Incident response plan written

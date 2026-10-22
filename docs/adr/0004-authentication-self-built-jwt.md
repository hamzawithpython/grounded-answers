# ADR 0004: Authentication — Self-Built JWT in FastAPI

**Status:** Accepted (amended 2026-10-22: suspended tenants, see "Suspended tenants")

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

SPEC open decision #2 asks how users log in. Requirements (SPEC §6.1, §7.1, §7.4): authentication for all users, RBAC per tenant, tenant isolation enforced by PostgreSQL Row-Level Security (RLS), Telegram linking later without redesign, an audit log, secrets only from environment variables, free tiers, and a security design we can explain line by line.

| Option | Verdict |
|---|---|
| **A. Self-built JWT in FastAPI** | **Chosen.** Auth lives beside the tenant checks, and we control claims and lifetimes. Cost: we own the security work. |
| B. Supabase Auth | Rejected. Its RLS model uses Supabase's `auth.uid()`, which doesn't fit our Postgres and `app_user` role. Free projects pause after a week idle ([Supabase pricing](https://supabase.com/pricing)). |
| C. Better Auth (Auth.js successor) | Rejected. TypeScript-first, and our API is Python. Auth.js is now maintained under Better Auth ([announcement](https://www.better-auth.com/blog/authjs-joins-better-auth)). |

## Decision

Build authentication in the FastAPI backend using vetted libraries only. No hand-rolled cryptography.

### Libraries (verified 2026-10-22 on PyPI)

| Library | Latest | Released | License | Purpose |
|---|---|---|---|---|
| `argon2-cffi` | 25.1.0 | 2025-06-03 | MIT | Argon2id password hashing |
| `PyJWT[crypto]` | 2.15.1 | 2026-09-28 | MIT | JWT sign/verify with EdDSA (uses `cryptography`) |
| `cryptography` | 50.0.2 | (PyPI) | Apache-2.0 OR BSD-3-Clause | Ed25519 primitives, pulled in by PyJWT's `crypto` extra |

*Alternative:* `pwdlib` 0.3.1 (MIT, released 2026-08-12) is a higher-level wrapper. It is newer, so we use `argon2-cffi` directly for a longer track record.
*Maintenance:* `argon2-cffi` has had no release since 2025-06. It is small and stable. We recheck it at each dependency review.

**Dependency rule (ADR 0002):** `pyproject.toml` uses `>=` ranges (`argon2-cffi>=25.1.0`, `PyJWT[crypto]>=2.15.1`). Exact versions and hashes live only in `uv.lock`.

### Tokens

**Access token (JWT, 15 min)**
- Algorithm **EdDSA (Ed25519)**. Smaller than RS256, and no RSA parameter choices to get wrong.
- Header `kid` selects the verification key.
- Claims: `sub` (user id), `tid` (tenant id), `role` (**hint only**), `iat`, `exp`, `iss`, `aud`, `jti`.
- Verification always pins `algorithms=["EdDSA"]`. Never accept `none`, and never let the header choose the algorithm (algorithm-confusion attack).
- Held in browser memory only (see Cookie design).

**Refresh token (opaque, 14 days)**
- `secrets.token_urlsafe(32)` (256 bits). Not a JWT, so there is nothing to forge.
- **Stored hashed:** the database holds `SHA-256(token)`. A database leak yields no usable tokens.
- Sent only as an `httpOnly` cookie.
- **Rotated on every use** within a *family*. The old token is marked used and a new one is issued.
- **Reuse detection:** a *used* token presented again, outside the grace window below, means theft or replay. Revoke the whole family and write an audit entry.
- **Logout** revokes the current family. **Logout-all** and **password reset** revoke every family for the user (within the user's tenant; see the single-membership rule below).

**Refresh race handling (concurrent tabs)**

Two tabs refreshing at the same moment would both present the same token, which looks like reuse. Two defenses:
1. **Single-flight in the frontend:** refreshes run under the Web Locks API (`navigator.locks.request("auth-refresh", …)`). Only one tab refreshes at a time; the others wait and then use the cookie the first one set.
2. **Server grace window, 10 s:** a token used less than 10 s ago gets `409 refresh_in_progress` with no revocation and no new token. The client retries. After 10 s, reuse triggers family revocation.

The normal case never hits the server race. The server also doesn't mistake a near-simultaneous request for theft.

### Per-request checks

The token's claims are hints. For every request:
1. Verify signature, `exp`, `iss`, `aud`, `alg`.
2. `SET LOCAL app.current_tenant = <tid>` for this transaction.
3. Query the user's **membership** for that tenant. It must exist and be active.
4. Authorize using the **role from the database**, never the claim.

Step 3 runs under RLS. A forged `tid` returns no row, so the request fails closed.

### Users are global; the two RLS exceptions

**`users` is global (no `tenant_id`).** One email is one person across tenants, so `users` is identity, not tenant data. Membership (`tenant_memberships`, with `tenant_id NOT NULL` and RLS) carries the tenant and role.

**v1 rule: one membership per user.** `UNIQUE(user_id)` on `tenant_memberships`. Logout-all and password reset then revoke sessions inside one tenant context, which avoids a third cross-tenant function. Relaxing this later needs a new ADR.

Because `users` has no tenant column, RLS on it is policy-based, not column-based (see "Tenant admin user management" below). Login and refresh are the only two paths that need *cross-tenant* or *pre-tenant* access, and they go through two narrow functions, the only RLS exceptions in the system:

| Function | Used for | Returns (minimum columns) | Why it exists |
|---|---|---|---|
| `auth_login_lookup(email)` | Login | `user_id`, `password_hash`, `user_active`, `tenant_id`, `tenant_status`, `membership_role`, `membership_active` | Login happens before tenant context exists. `tenant_status` was added in the 2026-10-22 amendment (suspended tenants). |
| `auth_refresh_lookup(token_hash)` | Refresh | `token_id`, `user_id`, `tenant_id`, `family_id`, `used_at`, `revoked_at`, `expires_at` | The opaque token doesn't say which tenant it belongs to. |

After either lookup, the app runs `SET LOCAL app.current_tenant`, and all further reads and writes go through normal RLS. The functions only read; rotation, revocation and writes always run as `app_user` under RLS.

**Hardening for both functions (applies to every `SECURITY DEFINER` function we write):**
- `SET search_path = pg_catalog, <our schema>` on each function. This prevents search-path hijacking, where a malicious object in an earlier schema shadows a table or function the function calls.
- Schema-qualified names inside the function body. No unqualified table references.
- `REVOKE EXECUTE ON FUNCTION … FROM PUBLIC` (Postgres grants EXECUTE to PUBLIC by default), then `GRANT EXECUTE … TO app_user` only.
- Tests: `app_user` can execute both functions; every other role gets `permission denied`.

### The definer role: `auth_definer`

A `SECURITY DEFINER` function runs as its owner. We give ownership of the two functions to a dedicated role, `auth_definer`, with these properties:

- **`NOLOGIN`.** It can never connect, so the function's privileges are not reachable by any client session.
- **No `BYPASSRLS`.** It is subject to RLS like any other role. `BYPASSRLS` would skip RLS on every table, so it is never used here.
- **Owns only the two functions.** It owns no tables.
- **Role-targeted RLS policies.** Only the tables and operations the functions need, each with a policy that applies to `auth_definer` alone:

| Table | Operation | Policy | Needed by |
|---|---|---|---|
| `users` | SELECT | `CREATE POLICY auth_definer_login ON users FOR SELECT TO auth_definer USING (true)` | `auth_login_lookup` |
| `tenant_memberships` | SELECT | `CREATE POLICY auth_definer_login ON tenant_memberships FOR SELECT TO auth_definer USING (true)` | `auth_login_lookup` |
| `refresh_tokens` | SELECT | `CREATE POLICY auth_definer_refresh ON refresh_tokens FOR SELECT TO auth_definer USING (true)` | `auth_refresh_lookup` |
| `tenants` | SELECT | `CREATE POLICY auth_definer_login ON tenants FOR SELECT TO auth_definer USING (true)` | `auth_login_lookup` (reads `status` only) |

- Column-level `GRANT SELECT` on only the columns listed in the function table above.
- No INSERT, UPDATE or DELETE for `auth_definer`. Writes always run as `app_user`.
- `users` has `ENABLE ROW LEVEL SECURITY`. The `auth_definer` policy above and the tenant-scoped `app_user` policies (below) are the only ways in. A mistaken grant does not expose other tenants' users.

**Why the policy is safe:** `USING (true)` applies only to the `auth_definer` role, and that role can run only the two functions, which return minimum columns for one email or one token hash.

**Tests (Step 6):**
- `auth_definer` cannot log in (`rolcanlogin = false`; a login attempt fails).
- `auth_definer` has no access to tables outside the table above (e.g. `audit_log`, `documents`, `tenants`): `permission denied` or zero rows.
- `app_user` cannot read `users` directly (`permission denied`), and cannot `SET ROLE auth_definer`.
- Both functions return only the listed columns.

### No role in the system has BYPASSRLS

- **Application and CLI roles:** `app_user`, `auth_definer`, and the migration role are never granted `BYPASSRLS`.
- **Migration role and the admin CLI's RLS treatment:** the migration role owns the tenant tables, and `FORCE ROW LEVEL SECURITY` applies to owners too. Platform-owner operations (creating tenants, cross-tenant evals) need the migration role to see all tenants. So each tenant table gets an explicit policy `TO migration_role USING (true) WITH CHECK (true)`. This is an explicit grant, not a bypass, and it's visible in the policy list. It is also the only cross-tenant access for operators. *New fact for review:* the migration role is unrestricted across tenants through these policies, which is why its credentials stay on the operator's machine or in a protected CI secret.
- **Superusers always bypass RLS.** The Docker `postgres` user is a superuser, so the app never connects as it. Step 6's superuser test documents this.

### Suspended tenants (amendment, 2026-10-22)

A tenant can be `suspended` (see `tenants.status` in ARCHITECTURE.md). Suspension blocks access without deleting data.

- **Login** (`auth_login_lookup`) returns `tenant_status`. For a suspended tenant, login returns the **same generic `401 Invalid email or password`** as a wrong password, with the same body and timing. Unauthenticated callers must not learn that a tenant exists or is suspended.
- **`tenants` RLS for `app_user`:** `FOR SELECT TO app_user USING (id = <current tenant>)`. The app reads its own tenant row and nothing else. No `INSERT`, `UPDATE` or `DELETE` grants. Platform changes go through the admin CLI (the migration role).
- **Per-request check:** `tenant_session` reads the tenant row in the same transaction as the membership check. A suspended tenant returns **`403 Tenant access is suspended`**. Only already-authenticated users reach this, so it reveals nothing to strangers.
- **Tests:**
  - Login to a suspended tenant returns the generic `401`, byte-identical to a wrong password.
  - An authenticated request for a suspended tenant returns `403`.
  - `app_user` sees only its own tenant row.
  - `app_user` cannot `UPDATE tenants`.
  - `auth_definer` reads `tenants.status` only (the `tenants` policy grants no other access).

### Tenant admin user management (`app_user`, web app)

Tenant admins create and manage employees through the web app, so `app_user` needs tenant-scoped access to `users`. The scope is enforced by policy, not by a definer function.

**Tenant predicate (fails closed).** With no tenant set, `current_setting(..., true)` returns NULL, so the predicate is false:

```sql
EXISTS (
  SELECT 1 FROM tenant_memberships m
  WHERE m.user_id = users.id
    AND m.tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
)
```

**Policies for `app_user` on `users`:**

| Operation | Policy | Notes |
|---|---|---|
| SELECT | `USING (<predicate>)` | Only users with a membership in the current tenant. |
| UPDATE | `USING (<predicate>) WITH CHECK (<predicate>)` | Cannot move a user into another tenant. |
| INSERT | `WITH CHECK (true)` | See the creation section below. The membership insert is the real gate. |
| DELETE | none | Deactivate with `active = false`. No hard deletes in v1. |

**Column grants for `app_user`:**
- `SELECT (id, email, display_name, active, created_at)`. **No SELECT on `password_hash`**, so the hash can never be read back through the app.
- `UPDATE (display_name, active)` for admin edits.
- `UPDATE (password_hash)` only for password changes. PostgreSQL allows a column to be written without being readable. The hash is produced by the app's Argon2 code, and only two code paths write it: the change-password endpoint and the reset-token endpoint. Both are tested.

**Creating employees (INSERT path): decision = policy, not a definer function.**

| | (i) Policy: `users` INSERT `WITH CHECK (true)` + tenant-scoped membership INSERT, one transaction | (ii) Narrow `SECURITY DEFINER` `create_tenant_user(...)` | (iii) Two separate writes, no transaction |
|---|---|---|---|
| **Risk** | An orphan `users` row can exist with no membership. Harmless: login requires an active membership. | A third definer function that *writes*. The largest privilege in the design. | Partial failure leaves orphan rows, with no rollback. |
| **Effort** | Low | Medium, plus extra tests | Low |

**Recommendation: (i).** The security boundary is the membership insert, not the user insert. Membership insert policy: `FOR INSERT TO app_user WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)`.

Two details make (i) work:
1. **The app generates the user id (UUID4) before the insert and does not use `RETURNING`.** `RETURNING` would need the new row to pass the SELECT predicate, which needs a membership row that doesn't exist yet. Order inside one transaction: insert `users`, then insert `tenant_memberships`. If either fails, both roll back.
2. The user insert needs `WITH CHECK (true)` only because of the ordering above. An orphan user cannot log in, so this is not a privilege escalation.

**Duplicate email.** `users.email` is globally unique, and an admin cannot see other tenants' users. So a duplicate is detected only by the unique constraint. To avoid revealing that the email exists elsewhere, the create endpoint returns the **same** generic response for every unique violation, whether the email belongs to another tenant or this one: `409 Could not create this user. Check the details and try again.`

*Residual risk:* an admin can still probe emails by repeating create attempts, because the global unique constraint is an oracle. Mitigations: rate limit on the create endpoint per admin, and every create attempt (success or failure) is written to `audit_log`. Tenant admins are authenticated, so this is weaker than anonymous enumeration, and we accept it for v1.

**Password reset is unauthenticated.** The user submitting a reset token has no tenant context yet. The reset link carries the tenant id as a **hint**. The reset-token row lookup runs under RLS with that tenant set, so a wrong or forged hint returns no row and the request fails with a generic `401`. The hint is never trusted for authorization. This is the same pattern as the membership check, and it needs no third definer function.

### Bootstrap: who creates the roles

Creating roles needs `CREATEROLE` (or superuser), and `ALTER FUNCTION … OWNER TO auth_definer` needs the migration role to be a member of `auth_definer`. Neither should be done inside an ordinary Alembic migration that the app role can see. So:

- **Bootstrap script** (`backend/scripts/bootstrap_roles.sql`, run once per database):
  1. `CREATE ROLE auth_definer NOLOGIN;`
  2. `CREATE ROLE app_user LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;` (password from a secret, never in the file)
  3. `GRANT auth_definer TO <migration_role>;` (needed for the owner change)
  4. Grants: `app_user` gets SELECT/INSERT/UPDATE/DELETE on tenant tables, and EXECUTE on the two functions only.
- **Locally:** the bootstrap runs as the Docker `postgres` superuser (docker-compose), once, from `docker compose exec`. Document in RUNBOOK.
- **On a managed host (Neon, Supabase or similar):** the provider's project owner role runs the bootstrap. Whether that role can `CREATE ROLE` and `ALTER … OWNER` on the chosen plan is **not verified yet**. We check before Step 4 and record the answer in this ADR.
- **Alembic migrations** create tables, policies and functions as the migration role. They do not create roles.

### Platform owner operations: admin CLI, not the web app

Creating tenants and running cross-tenant evals are platform-owner operations. They run through an **admin CLI** that uses the **migration role**, not through the web API. So the web app gets **no third RLS exception**.

- The CLI is operator tooling. It is never packaged into the web or worker container, and its credentials are never present in the web app's environment.
- Migration-role credentials are used only from the operator's machine or a CI job with protected secrets.
- SPEC §4 roles table updated to match.

### Key management

- Ed25519 private keys come from environment variables or secret files (`*_FILE`, read by pydantic-settings). Never committed.
- `AUTH_JWT_KEYS` (or `AUTH_JWT_KEYS_FILE`) holds `kid → key`. `AUTH_JWT_ACTIVE_KID` names the signing key.
- **Rotation:** add a key and make it active. Keep old keys for verification for at least 15 min (the access-token lifetime), then remove them.
- Only FastAPI verifies tokens, so there is no public JWKS endpoint in v1.

### Cookie and cross-site design

| | (a) Next.js BFF proxy | (b) One site, two subdomains | (c) Memory access token + `SameSite=None` refresh cookie |
|---|---|---|---|
| **How** | Browser talks only to Next.js, which forwards to FastAPI using the server-side cookie. | `app.example.com` and `api.example.com` share one *site*. Refresh cookie is `SameSite=Lax`. | Cookie is sent cross-site to the API. |
| **Browser risk** | None from third-party cookie rules. | None; same site. | Safari and Firefox block third-party cookies by default, so refresh can fail silently. *Current 2026 status not verified.* |
| **CSRF** | `SameSite=Lax` plus an `Origin` check in route handlers. | `SameSite=Lax` blocks cross-site cookie sends, plus an `Origin` check on `/auth/*`. | `SameSite=None` sends the cookie on every cross-site request. Depends on extra checks. Weakest. |
| **Cost** | Free. Extra hop per request (SPEC §7.3 p95 targets). Next.js holds user tokens. | A domain, about $11/yr (see below). | Free, weakest security. |
| **Build effort** | Medium: a proxy layer to write and test. | Low. | Low to build, high to secure. |

**Recommendation: (b).** FastAPI stays the only auth authority, and no proxy holds tokens. `SameSite=Lax` is the modern default and blocks cross-site cookie sends.

**Dev host: `127.0.0.1` everywhere.** Not `localhost`, for three reasons:
1. Browsers treat `localhost` and `127.0.0.1` as different hosts, so a cookie set by one isn't sent to the other.
2. Node resolves `localhost` to `::1` (IPv6) first with no fallback, while uvicorn binds `127.0.0.1` (IPv4).
3. Our DB and Valkey URLs already use `127.0.0.1`.

Settings: frontend `next dev -H 127.0.0.1` (the `dev` script), `NEXT_PUBLIC_API_URL=http://127.0.0.1:8000`, `CORS_ORIGINS=http://127.0.0.1:3000`. Whether `Secure` cookies are accepted on `http://127.0.0.1` is verified in Step 6, not assumed.

**CSRF protection for (b):**
1. Refresh cookie: `__Secure-refresh`, `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/auth`, host-only (no `Domain` attribute).
2. The access token is sent as `Authorization: Bearer`. Browsers don't attach it automatically, so it can't be CSRF'd.
3. `POST /auth/refresh` and `POST /auth/logout` reject requests whose `Origin` isn't in the allowlist.
4. Tests: cross-site request without cookie is rejected; bad `Origin` is rejected; `Bearer` request without token is rejected.

**CORS (required by (b)), implemented in this step:**
- Frontend: `fetch(url, { credentials: "include" })`.
- Backend: `allow_origins` is the exact list from `CORS_ORIGINS`, `allow_credentials=True`, never `*`. `allow_methods` is `GET, POST, PUT, DELETE`. `allow_headers` is `Authorization, Content-Type`. Extend the lists only when an endpoint needs it.

**Domain (decided):** bought at deployment time, not now. Option (b) needs one. Renewal is about $10.46 (Cloudflare) to $11.08 (Porkbun) per year for `.com`, from Porkbun's comparison, which lists Namecheap at $18.68 renewal ([source](https://porkbun.com/about/domain-cost-calculator/com)). Confirm on the registrar before buying.

**Free fallback:** option (a), the BFF proxy. Documented here so we can switch without changing the token design.

### Scope for v1

- **Admin-created accounts only.** No self-signup. Tenant admins create users.
- **Admin-triggered password reset.** The admin generates a one-time reset token, stored hashed, with a 24-hour expiry, and passes it to the user out-of-band. The reset revokes all of the user's refresh families.
- **No email sending** (v2).
- **Password policy:** minimum 12 characters, checked against a short list of common passwords. No composition rules.

### Login protection

**Lockout and rate limits (DoS trade-off).** A hard lock on the *account* alone lets anyone who knows an email lock that person out. So:
- **Primary lock, keyed on (email, IP):** after 5 failures within 15 min, that pair is locked for 15 min. An attacker on one IP can only lock out their own attempts.
- **Per-IP rate limit:** 20 login attempts per 10 min across all emails from one IP. Blocks password spraying. Sliding window in Valkey.
- **Per-account progressive delay:** after 5 failures, the delay grows 1 s, 2 s, 4 s, … capped at 30 s. Slows guessing from many IPs without a hard lock.
- **Account-wide hard lock at a high threshold:** 50 failures in 1 h, released by an admin. Last resort.

*Trade-off:* a lock keyed on (email, IP) is weaker against a distributed attacker, which is why the progressive delay and the high-threshold account lock exist. A lower account-wide threshold would be easier to abuse for denial of service.

**Other controls**
- **Generic errors:** every failure returns `401 Invalid email or password`. Unknown emails still run Argon2 against a dummy hash, so response timing doesn't reveal which emails exist.
- **Audit and logging:** successful and failed logins for a *known* user are written to `audit_log`, which has a tenant. For *unknown* emails there is no tenant, so the event goes to the application log only: event name, timestamp, and an **HMAC-keyed IP** (HMAC with a server secret, not a plain hash, because IPv4 addresses can be hashed exhaustively). No email address and no password in any log.

## Rationale

1. **Control:** we set lifetimes, claims, rotation and lockout rules. Those choices are the security story.
2. **Fit:** auth shares one Python service with the RLS tenant setting, so identity and tenant checks are one code path.
3. **Telegram later:** a `telegram_links(user_id, telegram_user_id)` table, with no auth redesign.
4. **Learning value:** every part can be explained and tested.
5. **Cost:** free, except the domain at deployment.

## Consequences

- **Positive:** no vendor lock-in; no free-tier pause; refresh-token reuse is detected on next use.
- **Negative:** we own the security work. Mitigation: libraries only, a test per rule, and a threat model in SECURITY.md (Session 2, Step 8).
- **Negative:** two intentional RLS exceptions (login and refresh lookups). *Amended by ADR 0007: a third, read-only function, `retention_due_tenant_ids()`, returns tenant ids only, for the purge worker.* Both are read-only `SECURITY DEFINER` functions with hardened `search_path`, execute-restricted to `app_user`, owned by the non-login `auth_definer` role, which has narrow policies and no `BYPASSRLS`. Documented in ARCHITECTURE.md and tested.
- **Negative:** the migration role is unrestricted across tenants through explicit policies (for operator work). Its credentials must stay off the web app and out of CI jobs that don't need them.
- **Negative:** v1 allows one tenant membership per user (`UNIQUE(user_id)`).
- **Negative:** the access token lives in JS memory, so an XSS bug could read it during its 15-minute life. Mitigations: strict CSP, no `dangerouslySetInnerHTML`, short lifetime.
- **Unverified:** whether managed Postgres hosts allow the bootstrap (`CREATE ROLE`, `ALTER … OWNER`). Checked before Step 4.
- **Data (Session 2, Step 3):** `users` (global, RLS on; policies for `auth_definer` login and for `app_user` tenant scope), `tenant_memberships` (tenant-owned, `UNIQUE(user_id)`), `refresh_tokens` (tenant-owned, hashed), `password_reset_tokens` (tenant-owned, hashed, 24 h), `audit_log` (tenant-owned). `telegram_links` later.

## Resolved open questions

1. **Unknown-email failures:** application log only, with an HMAC-keyed IP. No second audit store.
2. **RLS exceptions:** two read-only `SECURITY DEFINER` functions for login and refresh only, hardened as above.
3. **Browser third-party cookie policy:** not needed, because option (b) avoids cross-site cookies.
4. **Domain:** bought at deployment (about $11/yr). Option (a) BFF is the free fallback.
5. **Platform owner:** admin CLI with the migration role. No web-facing exception.
6. **BYPASSRLS:** no role has it. The definer role uses narrow policies instead. Superusers bypass RLS by design and are never used by the app.

## Alternatives considered

- **Supabase Auth, Better Auth, Auth.js:** see the table above.
- **RS256:** works, but larger keys and more parameter choices. EdDSA is simpler and supported by PyJWT.
- **Opaque access tokens (a DB lookup per request):** easier revocation. Membership is already checked per request, so this can be revisited later.
- **`BYPASSRLS` for the definer role:** rejected. It skips RLS on every table, needs a superuser to grant, and may be unavailable on managed hosts.

## Implementation notes

- Backend deps (Session 2): `argon2-cffi>=25.1.0`, `PyJWT[crypto]>=2.15.1` in `pyproject.toml`. Exact pins via `uv lock`.
- Config names (values never committed): `AUTH_JWT_ACTIVE_KID`, `AUTH_JWT_KEYS` / `AUTH_JWT_KEYS_FILE`, `AUTH_ACCESS_TOKEN_MINUTES=15`, `AUTH_REFRESH_TOKEN_DAYS=14`, `AUTH_REFRESH_GRACE_SECONDS=10`, `AUTH_LOG_HMAC_KEY`. The existing `CORS_ORIGINS` is reused for allowed origins. Placeholders go in `.env.example` (Step 4).
- Dev values: `NEXT_PUBLIC_API_URL=http://127.0.0.1:8000`, `CORS_ORIGINS=http://127.0.0.1:3000`, `next dev -H 127.0.0.1`. Documented in RUNBOOK.
- Tests (Step 6), auth:
  - Refresh: reuse detection revokes the family; grace-window retry does not; logout-all; expired and wrong-`alg` tokens rejected; forged `tid` returns 401.
  - Login: lockout per (email, IP); per-IP limit; progressive delay; generic error text; dummy-hash timing path.
  - Functions: both return only minimum columns and execute only for `app_user`; `auth_definer` cannot log in and has no access outside its policies.
  - Suspended tenants: generic `401` at login; `403` per request; `app_user` sees only its own tenant row and cannot update `tenants` (see "Suspended tenants").
  - Superuser bypass documented; audit row written; `Secure` cookie accepted on `http://127.0.0.1`.
- Tests (Step 6), tenant-admin user management:
  - `app_user` sees only users with a membership in the current tenant; zero rows with no tenant context; zero rows for another tenant.
  - `app_user` cannot SELECT `password_hash` (permission denied) or UPDATE `email` (permission denied).
  - UPDATE on another tenant's user affects 0 rows; moving a user into another tenant fails `WITH CHECK`.
  - Membership INSERT for another tenant's id is denied.
  - Create employee: user and membership commit together; a failing membership insert rolls back the user.
  - Duplicate email (same tenant and other tenant) returns the identical 409 body.
  - Reset token with a wrong tenant hint returns the generic 401.

## Related decisions

- ADR 0001 Monorepo Layout · ADR 0002 Dependency Managers · ADR 0003 Valkey (rate limits and lockout)
- Pending: ADR 0005 (database access layer), ADR 0006 (DB-dependent test strategy)

## Amendment (2026-10-22): employee creation through create_tenant_user (decision B)

**Decision.** `app_user` has no INSERT on `users` or `tenant_memberships`. Employees are created only by `grounded.create_tenant_user(p_email, p_display_name, p_role)`. The actor and the tenant are not parameters: they come from the session settings `app.current_user` and `app.current_tenant`, which the one auth code path sets together in the same transaction. It is a narrow `SECURITY DEFINER` function owned by a new `provisioning_definer` role (NOLOGIN, NOBYPASSRLS, owns only this function). It writes the user and the membership in one transaction, so a membership-less user cannot be created through the app.

**What the function checks, in order** (any failure raises the same error):
1. The tenant comes from the session setting `app.current_tenant`, never from a parameter. Likewise the actor, from `app.current_user`.
2. The actor (`app.current_user`) is an active `tenant_admin` of that tenant.
3. The email's domain is on `tenant_email_domains` for that tenant (see below).
4. The role is `employee` or `tenant_admin`, and the display name and email are well-formed.
5. The insert of the user and membership runs in a sub-transaction. A unique violation on the email raises the same error.

**Generic errors.** Every refusal returns SQLSTATE `P0001` and the message `could not create this user`. The caller cannot tell a wrong domain, a duplicate email (in this tenant or another), a non-admin actor, another tenant's admin, or a missing tenant from each other. Tests compare SQLSTATE and message across all six causes.

**Allowlist (squatting mitigation).** `tenant_email_domains` is a tenant-owned allowlist. `app_user` can read its own tenant's rows. Only `migration_role` (the operator and admin CLI) writes it. An admin can create accounts only at domains the operator has approved for that tenant.

**Defence in depth, not a hard boundary.** The actor id and the tenant come from session values the app sets (`app.current_user` and `app.current_tenant`). A compromised app process could send any actor id for the session tenant. The function's checks stop the cases a buggy or unprivileged caller would hit, but they do not stop an app that already controls the session. The hard boundary is that the app can only reach this function, not the tables behind it.

**Residual risks (accepted for v1):**
- **Email squatting inside an approved domain.** A tenant admin can create an account for any name at an approved domain, including a person who has not joined yet. Only email verification removes this, and v1 sends no email (v2).
- **Enumeration by the admin.** The global email uniqueness is an oracle, but only for an authenticated admin, and the generic error keeps the oracle to yes-or-no per attempt. Mitigation: rate limit on the create endpoint and an audit entry per attempt (application layer, Step 6+).
- **Orphan users.** Not possible through the function (one transaction). Possible only through direct database access by migration_role.

**Why not an INSERT policy on users (option A).** It would let app_user create any user, including orphans and other tenants' employees, with no tenant check on the user row itself (users has no tenant column). Rejected.

**Why not an invitation table (option C).** It moves squatting to acceptance time, where it is no less risky without email verification, and adds a table and a flow that v1 does not need.

Tests: `tests/db/test_create_tenant_user.py` (success, every refusal, identical refusals, no row created on refusal) and `tests/db/test_rls_insert.py` (cross-tenant INSERT refused on every table, including `audit_log` with a NULL actor).


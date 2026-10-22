# ADR 0012: Login, Access Tokens and Per-Request Checks (Session 3, Step 3a)

**Status:** Accepted. The cookie decision (section 6) is based on the browser test of 2026-10-22; Safari is an open gap.

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

ADR 0004 defines authentication: EdDSA access tokens, login with Argon2id, per-request membership
and tenant checks, and generic errors. Step 3 is split into three PRs. This ADR records 3a: keys,
login, and the authentication dependency. Refresh rotation and logout are 3b. Lockout, rate limits
and CSRF Origin checks are 3c.

## Decisions

### 1. Key management (decisions 1A, 2A)

- Ed25519 private keys live in one JSON file, outside the repository, named by `AUTH_JWT_KEYS_FILE`.
  Format: `{"active": kid, "keys": {kid: PKCS#8 PEM}}`.
- kid = `ed25519-` plus the first 16 hex characters of SHA-256 of the raw public key. The loader
  refuses a label that does not match its key.
- Dev generation: `scripts/make_jwt_keys.py`, which refuses to overwrite an existing file. The
  RUNBOOK restricts the file to the Windows user with `icacls`.
- `AUTH_JWT_ACTIVE_KID`, if set, must match the file's `active`. The file is the source of truth.
- The key file is git-ignored by name (`*jwt-keys*.json`) as a second guard.

### 2. Token details

- `iss` = `grounded-answers-api`, `aud` = `grounded-answers-web`. Constants in code.
- Required claims: `sub`, `tid`, `iat`, `exp`, `iss`, `aud`, `jti`. `role` is a hint only.
- Verification pins `algorithms=["EdDSA"]`, selects the key by `kid` from a fixed map, and refuses an
  unknown kid before any signature check.
- Leeway: 5 seconds (decision 3). Lifetime: 15 minutes.
- Tests cover: expired, wrong audience, wrong issuer, unknown kid, `alg: none`, HS256 signed with the
  public key (built by hand, since PyJWT refuses to sign that way), a tampered signature, a missing
  claim, and a malformed token. A planted removal of the algorithm pin was caught by the alg-none and
  HS256 tests.

### 3. Login flow

1. `auth_login_lookup(email)` on an anonymous transaction. The email is trimmed and lowercased.
2. Argon2 verify runs on every path, in a worker thread (`starlette.concurrency.run_in_threadpool`).
   An unknown email, or a user with no hash, is verified against a dummy hash created once at
   startup with the same parameters.
3. After the verify: user active, membership present and active, tenant status `active`. Every
   failure returns the same response: status 401, body `{"detail":"Invalid email or password"}`, and
   no extra headers. Tests compare the bytes.
4. Success, inside `begin_tenant_transaction`: insert the refresh token (32 random bytes, stored as
   SHA-256 only, with a new family id) and a `login.success` audit row with the keyed IP.
5. Failure for a known user with a tenant: a `login.failure` audit row with an internal reason
   (`password`, `user_inactive`, `membership_inactive`, `tenant_suspended`). The reason never reaches
   the client.
6. Failure for an unknown email: one application log line with the event, reason and the first 16 hex
   characters of the keyed IP. No email address.

### 4. Per-request checks (the authentication dependency)

1. Read `Authorization: Bearer <token>`, then verify it (section 2). Failure: 401 `Not authenticated`
   with `WWW-Authenticate: Bearer`.
2. Open `begin_tenant_transaction(tid, sub)`. The transaction's first statements set the context.
3. In the same transaction, read membership role, membership active, tenant status and user active.
   RLS limits the rows to the tenant, so a forged `tid` finds nothing: 401.
4. Inactive user or membership: 401. Suspended tenant: 403 `Tenant access is suspended`. ADR 0004
   shows this only to authenticated callers.
5. The role comes from the membership row, never from the token. A test forges `role: employee` for
   an admin and checks that the database role is returned.

The dependency is declared with `scope="function"`, so the commit happens before the response is sent
(ADR 0011).

### 5. Keyed IP and deployment

The keyed IP (HMAC-SHA256 with `AUTH_LOG_HMAC_KEY`) uses the direct client address (`request.client`).
Trusted `X-Forwarded-For` handling is for hosting, when a proxy sits in front of the API. Until then a
proxy's address would be keyed, and the audit rows would not show the user's address.

### 6. Refresh cookie and the browser test (decided)

- The cookie is `__Secure-refresh`, with `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/auth` and no
  `Domain`. Browsers reject a `__Secure-` cookie that lacks `Secure`.
- MDN says a `Secure` cookie may be set over plain `http:` on `localhost`, and that Safari does not
  support that exception. MDN does not mention `127.0.0.1`, so the question was tested directly.
- **Browser test, 2026-10-22, on `http://127.0.0.1:8765`:** a throwaway probe server set the same cookie
  (`Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/auth`) and a second route echoed the cookies received.

  | Browser | Version | Result |
  |---|---|---|
  | Google Chrome | 155.0.8059.40 (64-bit) | Cookie accepted and sent back on `/auth/check`: `{"cookie_names_received":["__Secure-refresh"]}` |
  | Mozilla Firefox | 157.0.1 (64-bit) | Cookie accepted and sent back on `/auth/check`: `{"cookie_names_received":["__Secure-refresh"]}` |
  | Safari | not tested | No Windows build. Known gap (see below). |

- **Decision:** development stays on plain HTTP at `http://127.0.0.1`. The cookie is always `Secure`
  in every environment. There is no development flag, no `AUTH_COOKIE_SECURE`, and no local HTTPS.
  The previously planned mkcert fallback is not needed, because both tested browsers accept the cookie.
- **Known gap: Safari.** Safari does not support the localhost exception that MDN describes. It was not
  tested, because no Windows build is available. Safari users on the dev setup would need a different
  approach (for example, local HTTPS). Revisit when a Mac is available for testing, or before any
  browser-facing release.

### 7. Password hashing off the event loop

Argon2 hashing and verification are CPU-heavy. Both run in a worker thread. A test records the
thread that verifies and the thread that runs the lookup, and requires them to differ.

### 8. Validation errors do not echo input

FastAPI's default 422 includes the rejected value. A global handler returns only `type`, `loc` and
`msg`. A test sends a password with a marker string and checks that the marker appears nowhere in the
response or in the logs.

## Residual risks (new in 3a)

- **R11, timing on known users.** A known user's failure writes an audit row (a database write), while
  an unknown email writes only a log line. The verify costs the same on both paths, but the write
  makes a known user's failure a little slower. An attacker who measures this precisely can learn which
  emails exist. Mitigation options for 3c: a database write on the unknown path, or a fixed delay on
  every failure. Not addressed in 3a.
- **R12, keyed IP behind a proxy.** Accepted until hosting is chosen (section 5).

## Consequences

- Positive: a forged or downgraded token is refused on every path tested; a database change to a role,
  membership or tenant takes effect on the next request; a bad input value cannot appear in responses
  or logs.
- Negative: a removed membership or suspended tenant takes effect on the next protected request, because
  section 4 reads both from the database every time. The access token itself still carries its 15-minute
  lifetime, but it grants nothing once the database check fails.
- Negative: the dev browser setup depends on the test in section 6.

## Related decisions

- ADR 0004 (the design this implements)
- ADR 0005 (psycopg async, selector loop)
- ADR 0011 (tenant transaction helper, `scope="function"`, expect_rowcount)

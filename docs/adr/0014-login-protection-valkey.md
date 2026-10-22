# ADR 0014: Login Protection and Refresh Limits in Valkey (Session 3, Step 3c)

**Status:** Accepted

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

ADR 0004 defines login protection: lockout keyed on (email, IP), a per-IP limit, a progressive delay per
account, a hard account lock, generic errors, and a dummy hash for unknown emails. ADR 0012 built the login
path without these limits. ADR 0013 built refresh without a rate limit. This ADR records how the limits are
built (Step 3c), and closes residual risks R11 (timing) and R14 (rate limits).

## Decisions

### 1. Limits (ADR 0004, as written)

| Scope | Limit | Window |
|---|---|---|
| Email + IP pair | 5 failures → locked 15 minutes | 15-minute counter |
| IP | 20 failed logins block the IP | 10-minute window |
| Account (email) | after 5 failures, each attempt waits 1, 2, 4 … s, capped at 30 s | 1-hour counter |
| Account, hard | 50 failures → locked 24 hours | 1-hour counter |
| Refresh, per IP | 300 requests | 10-minute window |

Refused attempts are not counted. A refusal never extends a lock.

The per-IP limit counts failed logins, not all attempts (ADR 0004 said "attempts"). An office behind one
NAT address would otherwise use its budget on successful logins.

### 2. Atomic Valkey counters (Lua)

Each failure is recorded by one Lua script (`RECORD_FAILURE_LUA`), run through `register_script`. The script
increments every counter, sets the locks, and sets an expiry on every key, in one server-side step. Concurrent
failures are therefore counted exactly, and a lock starts at exactly the fifth failure. A test sends 40
failures at once and checks the count is 40.

Lock values are absolute times in milliseconds, computed from the application clock. Windows are computed
from the same clock. Tests move time with a fake clock, so nothing sleeps.

### 3. Keys are keyed hashes (HMAC)

Every key uses HMAC-SHA256 of the email, the IP, or the pair. The key comes from a new setting,
`AUTH_RATE_LIMIT_HMAC_KEY`, separate from `AUTH_LOG_HMAC_KEY`, so rotating one leaves the other alone. A test
scans every `auth:rl:*` key and value and fails if a raw email or IP appears. Another test checks that every
key has an expiry (`PTTL` is never `-1`).

### 4. Valkey unavailable (decision 7B)

- **Login fails closed.** It returns 503 with `Retry-After: 30` and the body `{"detail":"Sign-in is temporarily unavailable. Try again shortly."}`.
  Each occurrence logs a warning: `login.limiter_unavailable action=fail_closed`.
- **Refresh fails open.** Rotation and reuse detection still protect the token. Each occurrence logs
  `refresh.limiter_unavailable action=fail_open`.
- Every Valkey call has a 200 ms socket timeout, so a slow Valkey cannot stall a login.

### 5. Same answer for known and unknown emails (decision 6)

- The limit checks run **before** the database lookup, for every email. A failure for an unknown email is
  recorded in the same counters as a failure for a known one.
- After 50 failures, a known email and an unknown email get byte-identical responses: status 429, the same
  body, and the same `Retry-After`. A test checks this. The two emails are attacked in alternation, so both reach
  their fiftieth failure at the same clock time.
- Status 429 for every limit and lock. `Retry-After` is the true remaining time in whole seconds, rounded up.

### 6. Refresh limit (decision 8)

300 refresh requests per IP per 10 minutes. There is no per-account refresh limit, because each refresh
already rotates the token. Refused refresh requests keep the cookie: they are not credential failures.

### 7. Hard lock: Valkey, 24 hours, released by the operator (decision 3A)

The hard lock stays in Valkey, keyed by HMAC(email), with a 24-hour expiry.

- **Why not Postgres.** A `account_locks` table could only be written for known users. Unknown emails have no
  tenant, so row-level security would block the write unless a new SECURITY DEFINER function were added. The
  lock would then exist for known emails only. After 50 failures a known email would get 429 and an unknown
  one 401, which reveals which accounts exist. Keeping the lock in Valkey lets both get the same 429.
- **Expiry.** The 24-hour expiry also bounds the deliberate-lockout denial of service: an attacker can lock an
  account for at most a day at a time.
- **Release.** `unlock-account TENANT EMAIL` (operator CLI) deletes the hard lock, the account delay and the
  current-hour counter, and writes an `account.unlock` audit row whose detail holds the count of keys removed,
  never the email. The CLI needs `REDIS_URL` and `AUTH_RATE_LIMIT_HMAC_KEY` in the shell, as the migration URL
  is. Valkey keys are removed first. If the audit row then fails, the command says so.
- **Limit of this design.** A Valkey flush or restart drops every lock, and every other limit with it. This
  is accepted, and it is recorded as residual risk R15. Production hosting must not flush Valkey casually.
- **No migration.** Migration 0005 is not needed.

### 8. Timing floor (R11, decision 11A)

Every login response, whatever the outcome, waits until 500 ms have passed since the request started
(`timing.py`, `TimingFloor`). The wait is `asyncio.sleep`, which holds no thread and uses no CPU.

**Measurement** (local machine, 40 requests per path, through the app with the floor disabled, Argon2 verify
included, against the local test database and Valkey):

| Path | Median | p95 | Max |
|---|---|---|---|
| Unknown email, wrong password | 170 ms | 232 ms | 239 ms |
| Known user, wrong password (includes the audit write) | 177 ms | 277 ms | 299 ms |
| Success (refresh row and audit row) | 173 ms | 246 ms | 291 ms |

The slowest sample was 299 ms. The floor is 500 ms, a margin of about 200 ms. The measurement includes the
test client's own overhead, and 40 samples are not a p99.9. **Re-measure with this script when the hardware,
the database, or the Argon2 parameters change.** If the worst case approaches the floor, raise the floor.

The floor is the setting `AUTH_LOGIN_FLOOR_MS` (default 500, bounds 100 to 5000). A value outside the bounds
stops startup. When a login's real work exceeds the floor, the app logs
`login.timing_floor_exceeded floor_ms=<n> work_ms=<n>`, with no email, IP or password. A slower machine
therefore shows up in the log instead of silently weakening the floor. Tests check the bounds, the warning, and
that no warning appears when the work is quicker than the floor.

The test `test_every_login_path_waits_for_the_timing_floor` checks that the three paths all end at the floor
(with a fake clock). The unit test `test_known_and_unknown_paths_end_at_the_same_floor` checks the same
property in isolation.

### 9. Response for locks and limits

Every refusal is `429 {"detail":"Too many attempts. Try again later."}` with `Retry-After`. A refusal does
not say whether the email exists.

## Deployment notes

- `AUTH_RATE_LIMIT_HMAC_KEY` is required at startup (at least 32 characters). Local development needs it
  in `backend\.env` (RUNBOOK, Auth keys), or the API refuses to start.
- Multiple API instances must have roughly synchronised clocks, because windows and locks use the
  application clock.
- The keyed IP still uses the direct client address (ADR 0012 note). Trusted `X-Forwarded-For` handling comes
  with hosting. Until then, a proxy would be counted as one IP, which makes the per-IP limit stricter.

## Residual risks (new in 3c)

- **R15, Valkey flush.** Losing Valkey drops every lock and counter, including hard locks. Mitigation: hosting
  must not flush Valkey casually, and the limits return on the next failures.
- **R16, deliberate lockout.** An attacker who knows an email can lock that account for 24 hours, using
  fifty failures. Accepted. The lock is bounded to 24 hours, and the operator can release it at once.
- **R11 and R14** are closed by this ADR (sections 8 and 6).

## Related decisions

- ADR 0004 (login protection, as designed)
- ADR 0012 (login, the path these limits wrap)
- ADR 0013 (refresh, which now has a per-IP limit)

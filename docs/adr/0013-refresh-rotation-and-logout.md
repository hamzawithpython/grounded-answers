# ADR 0013: Refresh Rotation, Reuse Detection and Logout (Session 3, Step 3b)

**Status:** Accepted

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

ADR 0004 defines refresh tokens: opaque, stored hashed, rotated on every use, with reuse detection and a
10-second grace window for concurrent tabs. ADR 0012 built login and access tokens. This ADR records the
refresh, rotation, reuse, and logout decisions (Step 3b).

## Decisions

### D1. Grace window: 409 `refresh_in_progress`

A token used less than 10 seconds ago returns 409 with the body `{"detail":"refresh_in_progress"}`. The
response does not change the cookie, and no family is revoked. The frontend's Web Locks already serialize
refreshes, so a 409 means "retry", not "failed".

### D2. Exact count on the rotation UPDATE; zero is valid for revocations

- The rotation UPDATE (`used_at` and `replaced_by` on the locked row) must affect exactly one row. Anything
  else is a bug, and `expect_rowcount` raises.
- Family revocation (`revoked_at` on every live token of a family) may affect zero rows, when a concurrent
  request already revoked it. Such statements use `affected_rows` and record the count in the audit detail.
  They do not use the exact-count check.
- Logout and logout-all are idempotent for the same reason.

### D3. Absolute family cap (option B)

A rotated token never expires later than 14 days after the family's first token. The expiry is
`min(now + 14 days, MIN(issued_at over the family) + 14 days)`. The rotated cookie's `Max-Age` is the time
left until that expiry, not 14 days. A family started 13 days ago gets about one day. A family can no longer be
kept alive indefinitely by rotation.

### D4. Suspended tenant or inactive membership on refresh (option A)

- Suspended tenant: 403 `Tenant access is suspended`, and the family is revoked.
- Inactive user or membership: 401, and the family is revoked.
- Both write a `refresh.refused` audit row with the reason. The revocation commits before the response is
  sent, so the refusal cannot be undone by the error path.

### D5. Audit only security events (option A)

Audit rows are written for: `refresh.reuse_detected`, `refresh.refused`, `logout`, and `logout_all`.
Ordinary rotations are not audited, to keep the table small.

### D6. Origin check on the cookie-bearing POSTs (option B)

`POST /auth/refresh` and `POST /auth/logout` require an `Origin` header that is on the CORS allowlist. A
missing or foreign origin returns 403 and does not spend the token. This closes the CSRF gap that 3a left
open: `SameSite=Lax` blocks most cross-site POSTs, but not every browser enforces it. `POST /auth/logout-all`
uses a bearer token, which browsers do not attach automatically, so it does not need the check.

## The race, and how it is closed

Two requests with the same token can arrive together, for example from two tabs.

1. Both look up the token by hash. The lookup takes no lock.
2. Both open a tenant transaction and run `SELECT ... FOR UPDATE` on the token row.
3. The first request gets the lock, rotates the token, and commits.
4. The second request waits on the lock. When it gets it, the row shows `used_at` set. It is within the grace
   window, so it returns 409.

The rotation UPDATE also has `WHERE used_at IS NULL` and an exact-count check (D2), so a second rotation
would fail even without the lock. The test suite proves both properties: with the lock removed, the race test
fails at its first assertion (request 2 does not wait).

## Logout

- `POST /auth/logout` revokes the cookie's family and clears the cookie (204). A missing or unknown cookie is
  also 204 and clears the cookie.
- `POST /auth/logout-all` (bearer token, through the 3a dependency) revokes every family of the user in this
  tenant. Other users' families are not touched. It clears the cookie (204).

## Consequences

- Positive: a stolen token is usable at most once, plus the 10-second window. Reuse after the window
  revokes the whole family. A rotated cookie can never outlive the family cap. Concurrent refreshes cannot
  both rotate.
- Negative: the grace window is a deliberate exposure. A stolen token that is replayed inside 10 seconds of its
  use returns 409, not a revocation. This is accepted in ADR 0004. It is listed as residual risk R13.
- Negative: each refresh writes to the database inside a row lock. Concurrent refreshes for one family wait for
  each other. That is the intended trade-off.

## Residual risks (new in 3b)

- **R13, grace window.** Within 10 seconds of a rotation, a replayed token gets 409 and does not revoke the
  family. Accepted in ADR 0004. Mitigation: the window is short, and the new token is the only usable one.
- **R14, rate limits.** Refresh and login are not rate limited yet. That is 3c.
- **R11 (from 3a)** remains open until 3c.

## Related decisions

- ADR 0004 (refresh tokens, reuse detection, grace window)
- ADR 0011 (`scope="function"`, `expect_rowcount`)
- ADR 0012 (login, access tokens, per-request checks)

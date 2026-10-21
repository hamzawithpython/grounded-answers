# Security — Threat Model and Controls

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

TBD: Add threat model for data, auth, multi-tenancy, and infrastructure in Session 2.

## Controls

- **Tenant Isolation:** Row-Level Security in PostgreSQL (enforced in database layer)
- **Secrets:** Environment variables only; `.env` never committed; gitleaks scans CI
- **Input Validation:** Pydantic models validate all inputs; file uploads size/type limited
- **Rate Limiting:** Per-user and per-tenant (TBD Session 2)
- **Audit Logging:** Admin actions logged with timestamps and user IDs (TBD Session 2)
- **CORS:** Allowlist-based, no wildcard (`*`)
- **Dependencies:** SHA-pinned GitHub Actions; Dependabot for updates; supply-chain scanning (gitleaks)

---

## Checklist Before Production

- [ ] Threat model complete (Session 2)
- [ ] Tenant isolation tests passing (RLS verified)
- [ ] Braces vulnerability patched or replaced
- [ ] Rate limiting deployed
- [ ] Audit logging tested end-to-end
- [ ] TLS/HTTPS enforced
- [ ] Secrets manager (AWS Secrets, Vault, or similar) deployed
- [ ] WAF (Web Application Firewall) configured
- [ ] DDoS protection enabled
- [ ] Incident response plan written

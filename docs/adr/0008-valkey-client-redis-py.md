# ADR 0008: Valkey Client Library — redis-py

**Status:** Accepted

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

ADR 0003 chose the Valkey server. The app still needs a Python client to talk to it. The client
is needed first for the `/ready` check (ADR 0009) and later for sessions and caching.

## Options considered

| | **A. redis-py (`redis`)** | B. valkey-py (`valkey`) |
|---|---|---|
| License | MIT | MIT |
| Latest stable | 8.1.0, 2026-07-30 | 6.2.0, 2026-10-12 |
| Release pace | About monthly in 2026 (8.0.0 May, 8.0.1 June, 8.1.0 July) | Slow: 6.1.1 Aug 2025, then 6.2.0 release candidates Feb and Mar 2026 |
| Python | 3.10+ | 3.11+ |
| Async | `redis.asyncio`, in the same package | `valkey.asyncio` |
| Server compatibility | Speaks the same protocol as Valkey (ADR 0003 keeps `redis://` URLs) | Built for Valkey |
| Pros | Largest user base and examples; fast fixes; the app code stays generic (ADR 0003) | Maintained by the Valkey project; name matches the server |
| Cons | Name says "Redis", which can confuse readers of ADR 0003 | Slower releases; smaller ecosystem |

## Decision

Use **redis-py 8.1.0** (`redis>=8.1.0` in `pyproject.toml`; the exact version is locked in `uv.lock`).

## Rationale

- Both libraries are MIT, so the licence reasoning in ADR 0003 holds either way.
- redis-py is actively released. valkey-py had a long gap between stable releases.
- ADR 0003 keeps app code generic: `REDIS_URL` with the `redis://` scheme. redis-py fits that
  without a rename.
- Switching is a small change: the `/ready` check uses only `ping()`, and the import is in one place
  (`main.py`, `readiness.py`).

## Verification (2026-10-22)

- PyPI pages for `redis` and `valkey` were read directly. Version facts come from those pages, and a
  summarising fetch was used for them, so re-check the version on pypi.org before a manual upgrade.
- Dependency rule (ADR 0002): 7-day cooldown before manual upgrades (Dependabot, `.github/dependabot.yml`).

## Consequences

- **Positive:** a mature client with a well-known async API.
- **Negative:** the import name (`redis`) does not match the server (`valkey`). This ADR and ADR 0003 explain it.
- **Revisit:** if valkey-py releases become regular, or if the project wants Valkey-branded dependencies.

## Related decisions

- ADR 0003: Valkey as the cache server
- ADR 0009: readiness checks, which are the first user of the client

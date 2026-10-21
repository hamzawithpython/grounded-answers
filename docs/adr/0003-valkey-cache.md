# ADR 0003: In-Memory Cache and Data Store — Valkey

**Status:** Accepted

**Date:** 2026-10-21

**Deciders:** Hamza Asif

## Context

The system requires an in-memory cache and data store for session management, caching, and job queues.
We must choose between Redis (now with source-available licensing) and Valkey (Redis's open-source fork).

## Background: Redis Licensing Changes

- **Redis ≤7.2:** BSD license (fully open source)
- **Redis 7.4+** (license change announced March 2024, effective from Redis 7.4): Moved to source-available licensing
  - RSALv2/SSPL: Allows internal use as cache/queue; restricts offering Redis as a managed service
  - AGPLv3 (added in Redis 8, 2025): OSI-approved copyleft; allows offering as managed service but with copyleft obligations if modified

## Valkey Fork

Valkey was forked from Redis 7.2.4 (the last BSD-licensed release) in March 2024 in response to Redis 7.4's licensing change.
- **License:** 3-Clause BSD (like Redis ≤7.2)
- **Governance:** Linux Foundation backed; community-driven
- **Compatibility:** Drop-in replacement for Redis (same APIs, commands, data structures)
- **Status:** Latest stable version 9.1.2 (verified on Docker Hub, ~1 month old)

## Decision

We will use **Valkey** for caching and sessions, pinned to Docker image `valkey/valkey:9.1.2-alpine3.24`.

The service name in `docker-compose.yml` remains `redis`, and the environment variable `REDIS_URL` uses the standard `redis://` URI scheme.
This keeps app code generic and allows swapping Redis/Valkey without code changes.

## Rationale

| Aspect | Redis 7.4+ | Valkey |
|--------|-----------|--------|
| License | RSALv2/SSPL (source-available) or AGPLv3 (open source, copyleft) | BSD (permissive, fully open) |
| Internal Cache Use | ✓ Allowed | ✓ Allowed |
| Offering as Managed Service | Restricted under RSALv2/SSPL; allowed under AGPLv3 with copyleft obligations | ✓ No restrictions |
| Governance | Redis Inc (company-controlled) | Linux Foundation (community) |
| Compatibility | N/A | Drop-in replacement for Redis |
| Maturity | Battle-hardened | 9.1.2 (forked from Redis 7.2.4 base) |

**Valkey chosen because:**
1. **Portfolio/Learning:** Valkey's BSD license aligns with open-source principles and is unambiguous in interviews ("I chose the open-source fork").
2. **No Licensing Ambiguity:** BSD means no need to reason about whether use case triggers restrictions. Deployment strategy is never constrained by licensing.
3. **API Compatibility:** Drop-in replacement; no app code changes needed. Swappable with Redis if future business needs arise.
4. **Long-term Support:** Linux Foundation backing ensures community-driven development.

## Consequences

- **Positive:** Fully open-source BSD license; no licensing ambiguity or vendor lock-in.
- **Positive:** App code uses generic `REDIS_URL` and `redis://` URIs, so switching back to Redis is a one-line change if needed.
- **Positive:** Forked from Redis 7.2.4 codebase; mature and battle-tested.
- **Negative:** Smaller ecosystem and fewer third-party integrations than Redis.

## Alternatives Considered

1. **Redis 7.4/8:** Mature, well-known. RSALv2/SSPL allow internal use without restriction, but source-available licensing creates deployment constraints. AGPLv3 adds copyleft obligations if modified and offered over network.

## Implementation Notes

- Docker image: `valkey/valkey:9.1.2-alpine3.24` (verified on Docker Hub, ~1 month old)
- Healthcheck command: `valkey-cli ping` (same as Redis)
- Service name in compose: `redis` (for app portability)
- Environment variable: `REDIS_URL=redis://127.0.0.1:6379/0` (unchanged)

## Related Decisions

- ADR 0001: Monorepo Layout
- ADR 0002: Dependency Managers

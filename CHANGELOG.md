# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Dependency and tooling changes are listed here too.

## [Unreleased]

### Changed
- Frontend: upgraded `eslint` from 9.x to 10.13.0 (devDependency). Lint, typecheck, tests, prod audit,
  and build all pass. `eslint.config.mjs` needed no changes.
  Known gap: `eslint-plugin-react`, `eslint-plugin-jsx-a11y` and `eslint-plugin-import` (bundled by
  `eslint-config-next@16.4.2`) don't yet declare ESLint 10 peer support. npm reports them as invalid peers
  and installs anyway. Verified working via planted violations on 2026-10-22. Review date 2026-12-01.
  Documented in `docs/SECURITY.md`.

## [0.1.0] - Session 1

### Added
- Monorepo scaffold (backend FastAPI, frontend Next.js 16), Valkey cache, docker-compose, CI/CD, Dependabot,
  gitleaks. See `docs/PROGRESS.md` for the full list.

# ADR 0002: Python and Node Dependency Managers

**Status:** Accepted

**Date:** 2026-10-21

**Deciders:** Hamza Asif

## Context

The project requires dependency management for two languages:
1. **Python backend:** FastAPI, embeddings, database ORM, testing, linting
2. **Node.js frontend:** Next.js, React, TypeScript, build tooling

We must choose tools that are fast, reproducible, and maintainable in a production environment.

## Decision

- **Python:** Use **uv** (first released February 2024; widely adopted since)
- **Node:** Use **npm** (default with Node.js)

### Python: uv Project Workflow

Dependencies declared in `pyproject.toml` ([project] table, PEP 621):
```toml
[project]
dependencies = [
    "fastapi==0.x.x",
    "sqlalchemy==2.x.x",
    # ...
]

[dependency-groups]
dev = [
    "pytest==7.x.x",
    "black==23.x.x",
    # ...
]
```

Commands:
```bash
uv sync --locked              # Install from uv.lock (fails if outdated)
uv add package-name           # Add a production dependency
uv add --dev dev-package      # Add a dev dependency
uv lock                        # Update uv.lock (cross-platform, with hashes)
```

Lock file is `uv.lock` (cross-platform, includes hashes for reproducibility).

### Node: npm

Install and lock dependencies with `npm`:
```bash
npm install                   # Install from package-lock.json
npm ci                        # Reproducible install in CI
npm install package-name      # Add a dependency
```

## Rationale

### Python: uv vs alternatives

| Aspect | uv | pip + venv | Poetry |
|--------|----|----|--------|
| Lock file | ✓ Yes (uv.lock) | ✗ No (requires pip-tools) | ✓ Yes |
| Speed | ✓ Very fast (Rust-based) | ✗ Slower | ✗ Slower |
| Learning curve | ✓ Familiar (drop-in pip replacement) | ✓ Familiar | ✗ New syntax |
| Maturity | ✓ First released Feb 2024; widely adopted | ✓ Standard library | ✓ Mature |
| Publishing/packaging | ✗ Not designed for this | ✗ Not built in | ✓ Yes (overkill) |

**uv chosen because:** Provides reproducible lock files with fast builds. Uses PEP 621 standard (`pyproject.toml`), the modern Python packaging format. uv's cross-platform lock file (with hashes) is more explicit than pip-tools. For a service application (not a published library), uv is simpler than Poetry and more reproducible than bare pip.

### Node: npm vs pnpm

| Aspect | npm | pnpm |
|--------|-----|------|
| Setup | ✓ Default with Node | ✗ Requires `npm install -g pnpm` |
| Monorepo support | ✓ Yes (workspaces) | ✓ Excellent |
| Speed | ✗ Slower | ✓ Much faster |
| Disk usage | ✗ Larger node_modules | ✓ Smaller (hard links) |

**npm chosen because:** The project has only one Node.js package (frontend). pnpm's workspace and speed benefits do not apply. npm is the default, reducing the number of tools to install, maintain, and explain in CI and on Windows development machines. npm is sufficient for this scope.

## Consequences

- **Positive:** uv provides fast, reproducible Python builds with minimal configuration.
- **Positive:** PEP 621 format is the modern Python standard; easier to maintain alongside packaging tools.
- **Positive:** npm is familiar to all JavaScript developers; zero new tooling overhead.
- **Positive:** Cross-platform lock files (uv.lock) with hashes ensure reproducibility across Linux (CI) and Windows/macOS (local).
- **Negative:** uv is newer than pip, though stable and widely adopted; less Stack Overflow content than pip.
- **Negative:** npm is slower than pnpm (acceptable for a single frontend package).

## Alternatives Considered

1. **Poetry for Python:** More mature, but adds TOML config complexity and slower builds. Overkill for a service application without publishing requirements.
2. **pip + venv:** Standard but requires pip-tools for a lock file; less explicit reproducibility than uv.lock.
3. **pnpm for Node:** Better for large monorepos with many packages. Not applicable here; adds one more tool to install and maintain.

## Related Decisions

- ADR 0001: Monorepo Layout (single repo for backend + frontend)

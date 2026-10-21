# ADR 0001: Monorepo Layout

**Status:** Accepted

**Date:** 2026-10-21

**Deciders:** Hamza Asif

## Context

The Grounded Answers project has a backend (Python/FastAPI) and frontend (Next.js) that need to coexist in version control. We must decide whether to use a single repository (monorepo) or separate repositories.

### Key Requirements
- End-to-end testing of tenant isolation across the full stack
- Automated CI/CD for both components
- Single source of truth for documentation, specs, and evaluation harness
- Simplified deployment orchestration

## Decision

We will use a **monorepo** with the following structure:
```
grounded-answers/
├── backend/          # FastAPI application
│   └── evals/        # Evaluation harness and test sets
├── frontend/         # Next.js application
├── docs/             # Documentation, ADRs, specs, EVAL.md results
├── .github/          # Shared CI/CD workflows
└── ...
```

Both components can be deployed independently, but share a single CI/CD pipeline and version control history.

## Rationale

| Aspect | Monorepo | Separate repos |
|--------|----------|---|
| End-to-end tenant isolation testing | ✓ Easy in one workflow | ✗ Cross-repo complexity |
| Deployment independence | ✓ Yes, with separate workflows per component | ✓ Yes |
| Documentation clarity | ✓ Single source of truth | ✗ Duplicated specs |
| CI/CD complexity | ✓ One shared workflow | ✗ Two independent workflows |
| Single language coordination | ✗ Must manage Python + Node in one workflow | ✓ Separate, simpler workflows |

For a portfolio project with security/compliance requirements (tenant isolation), the ability to test the entire stack in CI outweighs the complexity of coordinating two languages.

## Consequences

- **Positive:** Unified CI/CD for security tests; single docs repository; evaluation harness colocated with backend.
- **Positive:** Clear folder structure reflects the system architecture; easier to onboard and explain.
- **Positive:** Single version control history for related changes across backend and frontend.
- **Negative:** One CI workflow must coordinate Python and Node jobs (linting, type checking, tests). More complex than separate repos, but manageable with GitHub Actions job matrices.

## Alternatives Considered

1. **Separate repos:** Better for independent deployment schedules, but makes end-to-end security testing harder.
2. **Monorepo with npm workspaces:** Not applicable; backend is Python, frontend is Node.

## Related Decisions

- ADR 0002: Dependency Managers (uv for Python, npm for Node)

# CLAUDE.md — Working rules for the Grounded Answers project

Read `docs/SPEC.md` and `docs/PROGRESS.md` at the start of every session.

## About me
- Hamza: intermediate developer, fresh CS graduate. I want to LEARN while building, not just get code.
- Environment: Windows, PowerShell, VS Code. Give PowerShell commands, not bash, unless inside Docker.
- Goal: a production-grade, full-stack portfolio project I can explain line by line in an interview
  and sell to clients.

## Critical principle
**Always verify versions and 'latest' claims online; never rely on training-data memory.** GitHub Actions, Docker images, and tool releases change frequently. Check official repositories/registries when pinning dependencies. This ensures accuracy and builds good habits for production work.

## Teaching mode (always on)
1. Before any change: state what you will do, why, and which files it touches.
2. For every design decision: give 2–3 options in a short table (pros, cons, cost), recommend one,
   then STOP and wait for my choice. Never decide architecture for me silently.
3. After each change: explain what changed, how it works, and the exact command to verify it,
   including the expected output.
4. Define each technical term the first time it appears.
5. When something fails: explain what happened, why it happened, and the exact next action.
   Do not repeat earlier steps unless needed.
6. Work in small steps: one feature or module per step, then stop for my review.

## Quality gates (never skip, never weaken)
- No feature without tests. Run tests, lint, and type checks before saying a step is done.
- Never hardcode secrets, keys, or passwords. Use `.env` (git-ignored) and keep `.env.example` updated.
- Validate all inputs. Handle errors explicitly. No bare `except`. No silent failures.
- Never log secrets or personal data.
- Tenant isolation is enforced in the database (Row-Level Security) and covered by tests.
- Never disable a failing test, security check, or lint rule to make CI pass. Fix the cause or ask me.
- Do not add a dependency without telling me why, its license, and an alternative.
- Do not commit or push without my approval. Use Conventional Commits.
- Never kill processes broadly (e.g., `taskkill /IM node.exe`). Stop only the process you started, by its PID, and tell me first.

## Documentation (update in the SAME step as the code)
- `docs/SPEC.md` — scope (only change with my approval)
- `docs/ARCHITECTURE.md` — system design, data model, diagrams (Mermaid)
- `docs/adr/NNNN-title.md` — one Architecture Decision Record per decision I make
- `docs/SECURITY.md` — threat model and controls
- `docs/EVAL.md` — evaluation method and latest results
- `docs/RUNBOOK.md` — run, deploy, debug, roll back
- `docs/LEARNING_LOG.md` — what I learned this session, in plain words, with interview-style Q&A
- `docs/PROGRESS.md` — what is done, what is next, open questions (update at the end of every session)
- `CHANGELOG.md` — summary of changes

## Migrations (hard rule)
- Once a migration has been applied to any persistent database (dev or prod), never edit it. Create a new migration instead. Edits are allowed only for migrations that exist solely in ephemeral test databases.

## Usage efficiency (my Claude usage is limited)
- Keep explanations clear but concise. No repeating content I already have.
- Read only the files needed for the current step.
- Prefer one complete, correct change over many partial attempts.
- At the end of each session, update `docs/PROGRESS.md` so the next session can start quickly.
- When waiting for CI, start ONE monitor per run. Don't start duplicate monitors or repeat results already reported.

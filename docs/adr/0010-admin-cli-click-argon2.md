# ADR 0010: Admin CLI (Click) and First-Admin Password Hashing (argon2-cffi)

**Status:** Accepted

**Date:** 2026-10-22

**Deciders:** Hamza Asif

## Context

ADR 0004 puts platform-owner work (creating tenants, managing the email-domain allowlist,
creating the first tenant admin) in an admin CLI that uses `migration_role`. The CLI is
operator tooling. It is not part of the web app, and its credentials must never reach the web
app's environment.

Creating a tenant admin means storing a password hash. ADR 0004 chose `argon2-cffi` for that,
and this ADR records the parameters we rely on.

## Options considered

### CLI library

| | **A. Click 8.5.0** | B. Typer 0.27.3 | C. argparse (standard library) |
|---|---|---|---|
| **License** | BSD-3-Clause (LICENSE file checked) | MIT (LICENSE file checked) | PSF (standard library) |
| **New packages** | None. Already installed through `uvicorn[standard]`. | `rich`, `shellingham`, `annotated-doc`, plus Click | None |
| **Commands and options** | Decorators | Typed from function signatures | Hand-written parser |
| **Tests** | `CliRunner` | `CliRunner` (from Click) | Call the parser yourself |
| **Maintainer** | Pallets | FastAPI's author | Python core team |

**Decision: A (Click).** It is already in the environment through uvicorn, so declaring it adds no
package. Typer is built on Click, so choosing Typer would add Click plus three more packages to a tool
that handles the migration credential. argparse would add no packages but more hand-written
parsing code.

### Password hashing

`argon2-cffi` (ADR 0004) is the hashing library. The alternative `pwdlib` is newer and wraps
the same algorithm; ADR 0004 already chose the longer-tested library.

## Decision

### Click, declared in the `operator` group

- `backend/pyproject.toml`: `[dependency-groups] operator = ["click>=8.5.0"]`. The range is a floor;
  the exact version is in `uv.lock`.
- Run the CLI with: `uv run --group operator python -m grounded_answers.admin_cli`.
- CI installs the group (`uv sync --locked --group operator`), so the CLI tests exercise the
  declared dependency rather than one that happens to come from uvicorn.

**What the group does and does not do.** Click is also a dependency of `uvicorn[standard]`, so the
runtime environment has Click whether or not the group is installed. The group does not keep
Click out of the web image. What keeps the CLI out of the web image is that the CLI is a separate
module that the web app never imports, and the web and worker containers are not meant to run it.
`uv tree --invert --package click` shows both paths: through uvicorn, and through the `operator`
group.

### argon2-cffi and its defaults

- `argon2-cffi>=25.1.0` (MIT, released 2025-06-03; PyPI latest 25.1.0 checked 2026-10-22).
- Transitive packages, verified on PyPI and in each LICENSE file:
  - `argon2-cffi-bindings` 26.1.0 (MIT, released 2026-08-20)
  - `cffi` 2.1.1 (MIT, released 2026-08-03)
  - `pycparser` 3.11 (BSD-3-Clause header, released 2026-10-09)
- Default parameters, read from argon2-cffi's `profiles.py` (RFC 9106 second recommended profile)
  and pinned by `tests/test_passwords.py`:

| Parameter | Value |
|---|---|
| Type | Argon2id |
| `time_cost` | 3 iterations |
| `memory_cost` | 65536 KiB (64 MiB) |
| `parallelism` | 4 |
| `hash_len` | 32 bytes |
| `salt_len` | 16 bytes |

The hash string carries its own parameters, so a later change to the defaults does not break
existing hashes. A test fails if the library's defaults change, so the change is reviewed.

### Password policy (ADR 0004, "Scope for v1")

- At least 12 characters and at most 256. No composition rules.
- Refused if the casefolded password is on the common-password list.
- The list is SecLists `10k-most-common.txt`, pinned to commit `913b327317496d062bcc7cace524aaad8a693be2`
  and vendored under `backend/src/grounded_answers/data/` (MIT; see the `README.md` there).
- **Deviation from ADR 0004's wording.** ADR 0004 says "a short list of common passwords". We use the
  10,000-entry list, because a short list misses common passwords of 12 or more characters. The
  policy's minimum length already excludes most short entries.

### Operator credential rules (ADR 0004)

- `MIGRATION_DATABASE_URL` is read from the process environment only. The CLI never reads
  `backend/.env`, so `Settings` never sees it, and the `Settings` rule (unknown keys are refused)
  still holds.
- If the URL has no password, the CLI asks for one with `getpass` (no echo).
- `create-admin` checks the tenant and the email domain (read-only) before any password prompt, so a
  wrong domain never asks for a password. The write transaction checks them again.
- The first admin's password is asked for twice. The two entries must match, and then the
  policy is checked. Passwords are never command-line arguments, so they do not reach shell
  history or the process list.
- The CLI checks `SELECT current_user` before any write, and refuses unless it is `migration_role`.
  A wrong URL therefore cannot make platform changes.
- Errors show the exception class, not driver text, because driver text can contain the host.

### Writes and the audit log

- Every command runs in one transaction. A failure rolls the whole command back.
- `create-admin` writes the user row, the membership, and the audit row together. A failed
  membership insert rolls back the user (tested).
- Audit rows have `actor_user_id = NULL`, because the operator is not a user of any tenant. The
  detail holds the domain or the role only, never an email or a password.
- Removing a domain checks the result: a `DELETE` that matches no row is a refusal, not a
  success. This is the rowcount rule from PROGRESS (Session 3 carry-over).

### Migrations: no new migration needed

Migration `0003_policies_and_definers` gives `migration_role` a `FOR ALL` policy on every
operator table, including `users`, `tenants`, `tenant_memberships`, `tenant_email_domains` and
`audit_log`. The DB tests confirm the CLI can write each of them. No migration was added, and
migration 0005 is not needed.

### Database access in Step 1

The CLI runs explicit SQL through SQLAlchemy `text()` with bind parameters. The ORM models arrive
in Session 3, Step 2 (ADR 0005). The SQL matches what the migrations define, and Step 2 can
replace it without changing behaviour.

## Consequences

- **Positive:** no new package for the CLI library. Argon2id parameters are pinned by a test. The
  credential never appears in a file, an argument, or the logs.
- **Negative:** operator actions are not attributed to a person. Audit rows have no actor. This is
  residual risk R10 in SECURITY.md. It is acceptable while there is one operator, and it should be
  revisited before a second operator is added.
- **Negative:** the Argon2 hash is computed in Python, which holds the plaintext password in
  memory until the function returns. Python cannot reliably erase it, so this is accepted.
- **Negative:** the CLI needs a running database and a migrated schema. On the dev database this
  means `alembic upgrade head` first (RUNBOOK).
- **Windows:** the CLI runs on the selector event loop from `grounded_answers.runtime`, like the
  app and migrations (ADR 0005).

## Alternatives considered

- **Typer:** rejected for Step 1. It is a good library, but it adds three packages on top of Click
  for little gain in a four-command tool.
- **argparse:** rejected. Zero packages, but more hand-written code in a tool that handles the
  migration credential.
- **Printing a one-time reset token for the first admin:** deferred. The reset endpoint is built in
  Session 3, Step 3, so the first admin could not log in until then. Hashing the password now lets the
  first admin log in as soon as the login endpoint exists.
- **`pwdlib`:** rejected for now. ADR 0004 already chose `argon2-cffi`, which has the longer track record.

## Related decisions

- ADR 0004 (admin CLI with `migration_role`; argon2-cffi; password policy; allowlist)
- ADR 0005 (database access; selector loop on Windows)
- ADR 0006 (test database; the DB suite runs through the same logins)

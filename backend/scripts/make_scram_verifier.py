"""
Print a SCRAM-SHA-256 verifier for one role's password (ADR 0006 amendment).

The password is typed at a hidden prompt, twice. Only the verifier is printed, so
it can be captured into an environment variable. The plaintext is never printed.

Usage (BOOTSTRAP_ADMIN_URL points at the maintenance database as the cluster admin):

    uv run --directory backend python scripts/make_scram_verifier.py app_user
"""

import getpass
import os
import sys

import psycopg

from grounded_answers.scram import build_verifier

LOGIN_ROLES = ("migration_role", "app_user", "worker_user")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in LOGIN_ROLES:
        print(f"usage: make_scram_verifier.py {{{'|'.join(LOGIN_ROLES)}}}", file=sys.stderr)
        return 2

    role = argv[1]
    admin_url = os.environ.get("BOOTSTRAP_ADMIN_URL")
    if not admin_url:
        print("BOOTSTRAP_ADMIN_URL is not set", file=sys.stderr)
        return 2

    first = getpass.getpass(f"Password for {role}: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        print("Passwords do not match.", file=sys.stderr)
        return 1

    try:
        with psycopg.connect(admin_url) as conn:
            verifier = build_verifier(conn, role, first)
    except ValueError as exc:
        print(f"Rejected: {exc}", file=sys.stderr)
        return 1

    print(verifier)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

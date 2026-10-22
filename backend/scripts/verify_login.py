"""
Verify that login roles can log in to the dev database (RUNBOOK, "Verify login").

Usage (passwords are typed at a hidden prompt, one per role):

    uv run --directory backend python scripts/verify_login.py app_user worker_user migration_role

Prints LOGIN OK or LOGIN FAILED per role. Exit code 0 only if every role logs in.
"""

import getpass
import sys

from grounded_answers.login_check import check_login

LOGIN_ROLES = ("migration_role", "app_user", "worker_user")
DEV_HOST = "127.0.0.1"
DEV_PORT = 5432
DEV_DATABASE = "grounded_answers"


def main(argv: list[str]) -> int:
    roles = argv[1:]
    if not roles or any(role not in LOGIN_ROLES for role in roles):
        print(f"usage: verify_login.py {{{'|'.join(LOGIN_ROLES)}}} [...]", file=sys.stderr)
        return 2

    all_ok = True
    for role in roles:
        password = getpass.getpass(f"Password for {role}: ")
        # check_login runs for every role even after a failure, so each result is printed.
        all_ok = (
            check_login(role, password, host=DEV_HOST, port=DEV_PORT, database=DEV_DATABASE)
            and all_ok
        )
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

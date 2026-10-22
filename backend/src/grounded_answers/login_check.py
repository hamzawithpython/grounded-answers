"""
Connect as one login role and report the result (RUNBOOK, "Verify login").

The check runs SELECT 1 as that role. It prints one line and never prints the password or the
driver's message, because messages can include hosts.
"""

import psycopg


def check_login(
    role: str,
    password: str,
    *,
    host: str,
    port: int,
    database: str,
) -> bool:
    """Return True if role can log in and run SELECT 1. Prints LOGIN OK or LOGIN FAILED."""
    try:
        with psycopg.connect(
            host=host,
            port=port,
            dbname=database,
            user=role,
            password=password,
            connect_timeout=5,
        ) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        print(f"LOGIN FAILED {role}: {type(exc).__name__} (sqlstate {exc.sqlstate})")
        return False
    print(f"LOGIN OK {role}")
    return True

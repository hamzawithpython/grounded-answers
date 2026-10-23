"""
Safety guard for the test database (ADR 0006, section 3).

The fixture drops and recreates a database through TEST_ADMIN_DATABASE_URL. This module
decides, before anything is connected or dropped, whether that is safe. It never connects.
"""

from collections.abc import Iterable
from urllib.parse import urlsplit

# The only database the fixture may drop. A constant, not read from any URL, so that no
# environment variable can point the drop at another database.
TEST_DB_NAME = "grounded_answers_test"

# The round-trip test builds and drops its own database (its own schema lifecycle, so it can
# never affect the session database). Only these two names may ever be dropped by the tests.
ROUNDTRIP_DB_NAME = "grounded_answers_test_roundtrip"
SCRATCH_DB_NAMES = frozenset({TEST_DB_NAME, ROUNDTRIP_DB_NAME})

# The admin URL must connect to the maintenance database, never to a database we keep.
MAINTENANCE_DB_NAME = "postgres"

# The dev cluster listens on this port (docker-compose.yml, service postgres). A test run must never reach it, even
# when no dev URL is configured to compare against (the failure that reset dev role passwords, 2026-10-23).
DEV_CLUSTER_PORT = 5432


class GuardError(RuntimeError):
    """Raised before anything is connected to or dropped. The message names the failed check."""


def database_name(url: str) -> str:
    """The database part of a URL, for example 'postgres' for postgresql://u:p@h:5432/postgres."""
    return urlsplit(url).path.lstrip("/")


def endpoint(url: str) -> tuple[str, int | None]:
    """
    Host and port of a URL, with localhost and 127.0.0.1 treated as one host.

    Both names reach the same loopback server, so a dev URL written with one name must still
    match an admin URL written with the other.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    return ("127.0.0.1" if host == "localhost" else host, parts.port)


def check_targets(admin_url: str, dev_urls: Iterable[str] = ()) -> None:
    """
    Refuse unsafe targets. Returns normally only when every check passes.

    Checks (ADR 0006, section 3; ADR 0006 amendment for the separate test cluster):
      1. The admin URL connects to the maintenance database, not to a dev or test database.
      2. No dev URL is reused as the admin URL.
      3. The admin URL is not on the same host and port as a dev URL. The test cluster runs on
         its own port (5433), so a test run cannot reach the dev cluster's roles.
      4. No dev URL names the test database (a dev URL must never point at the test target).
      5. The test target name is exactly TEST_DB_NAME and ends in '_test'.
    """
    if not (TEST_DB_NAME == "grounded_answers_test" and TEST_DB_NAME.endswith("_test")):
        raise GuardError("guard: TEST_DB_NAME must be 'grounded_answers_test'")

    admin_db = database_name(admin_url)
    if admin_db != MAINTENANCE_DB_NAME:
        raise GuardError(
            f"guard: TEST_ADMIN_DATABASE_URL must connect to '{MAINTENANCE_DB_NAME}', "
            f"not '{admin_db}'"
        )

    for dev_url in dev_urls:
        if dev_url == admin_url:
            raise GuardError("guard: TEST_ADMIN_DATABASE_URL is the same as a dev URL")
        if endpoint(dev_url) == endpoint(admin_url):
            raise GuardError(
                "guard: TEST_ADMIN_DATABASE_URL has the same host and port as a dev URL "
                "(use the test cluster on port 5433, docs/RUNBOOK.md)"
            )
        if database_name(dev_url) == TEST_DB_NAME:
            raise GuardError(f"guard: a dev URL names the test database '{TEST_DB_NAME}'")

    # Unconditional: the dev port is refused even when no dev URL is configured to compare against.
    if endpoint(admin_url)[1] == DEV_CLUSTER_PORT:
        raise GuardError(
            f"guard: TEST_ADMIN_DATABASE_URL uses port {DEV_CLUSTER_PORT}, the dev cluster. "
            "The test cluster is on port 5433 (docs/RUNBOOK.md)"
        )


def check_scratch_database(name: str) -> None:
    """Refuse to drop or create any database name that is not one of the scratch names."""
    if name not in SCRATCH_DB_NAMES:
        raise GuardError(
            f"guard: '{name}' is not a scratch test database; tests may only use "
            f"{sorted(SCRATCH_DB_NAMES)}"
        )


def check_same_cluster(admin_system_id: str, psql_system_id: str) -> None:
    """
    The admin connection and the psql command must reach the same PostgreSQL cluster (ADR 0006).

    The bootstrap runs through psql and sets the login roles' passwords. A PSQL_COMMAND that names another container
    would set them on that cluster. Both identifiers come from pg_control_system(). Empty means unknown, so refuse.
    """
    if not admin_system_id or not psql_system_id:
        raise GuardError(
            "guard: could not read the cluster identity from the admin URL or PSQL_COMMAND"
        )
    if admin_system_id != psql_system_id:
        raise GuardError(
            "guard: PSQL_COMMAND reaches a different PostgreSQL cluster than TEST_ADMIN_DATABASE_URL. "
            "Nothing was changed."
        )

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

# The admin URL must connect to the maintenance database, never to a database we keep.
MAINTENANCE_DB_NAME = "postgres"


class GuardError(RuntimeError):
    """Raised before anything is connected to or dropped. The message names the failed check."""


def database_name(url: str) -> str:
    """The database part of a URL, for example 'postgres' for postgresql://u:p@h:5432/postgres."""
    return urlsplit(url).path.lstrip("/")


def check_targets(admin_url: str, dev_urls: Iterable[str] = ()) -> None:
    """
    Refuse unsafe targets. Returns normally only when every check passes.

    Checks (ADR 0006, section 3):
      1. The admin URL connects to the maintenance database, not to a dev or test database.
      2. No dev URL is reused as the admin URL.
      3. No dev URL names the test database (a dev URL must never point at the test target).
      4. The test target name is exactly TEST_DB_NAME and ends in '_test'.
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
        if database_name(dev_url) == TEST_DB_NAME:
            raise GuardError(f"guard: a dev URL names the test database '{TEST_DB_NAME}'")

"""Connection details for the session test database (shared by the fixture and the tests)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlunsplit

from .guard import TEST_DB_NAME

# Roles that log in. Each gets a random password per session.
LOGIN_ROLES = ("migration_role", "app_user", "worker_user")


@dataclass(frozen=True)
class DbHandle:
    """Connection details for the session's test database."""

    # repr=False: a traceback or log line must never print a password or a URL that contains one.
    admin_url: str = field(repr=False)
    base: SplitResult = field(repr=False)
    passwords: Mapping[str, str] = field(default_factory=dict, repr=False)
    verifiers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def url_for(self, role: str, password: str | None = None) -> str:
        """URL that logs in as role to the test database. Without a password, the URL has none."""
        auth = role if password is None else f"{role}:{password}"
        netloc = f"{auth}@{self.base.hostname}:{self.base.port}"
        return urlunsplit((self.base.scheme, netloc, f"/{TEST_DB_NAME}", "", ""))

    def admin_test_url(self) -> str:
        """Cluster-admin URL pointing at the test database (for catalog checks)."""
        return urlunsplit((self.base.scheme, self.base.netloc, f"/{TEST_DB_NAME}", "", ""))

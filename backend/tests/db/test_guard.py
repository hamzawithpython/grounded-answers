"""Guard tests. No database needed: each refusal must happen before any connection."""

import psycopg
import pytest

from .guard import (
    ROUNDTRIP_DB_NAME,
    TEST_DB_NAME,
    GuardError,
    check_same_cluster,
    check_scratch_database,
    check_targets,
    database_name,
    endpoint,
)

# The test cluster (docker compose --profile test, port 5433). Never the dev cluster's port.
ADMIN_OK = "postgresql://postgres:admin-secret@127.0.0.1:5433/postgres"
DEV_URL = "postgresql+psycopg://postgres:dev-secret@127.0.0.1:5432/grounded_answers"


@pytest.fixture
def no_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the guard tries to connect. Refusals must not reach the server."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("guard attempted a connection")

    monkeypatch.setattr(psycopg, "connect", refuse)


def test_correct_targets_pass(no_connections: None) -> None:
    """Admin URL on the maintenance database, dev URL on the dev database: allowed."""
    check_targets(ADMIN_OK, [DEV_URL])


def test_admin_url_on_dev_database_is_refused(no_connections: None) -> None:
    """The DROP runs through the admin URL, so its database must be the maintenance one."""
    admin_on_dev = "postgresql://postgres:admin-secret@127.0.0.1:5432/grounded_answers"
    with pytest.raises(GuardError, match="must connect to 'postgres'"):
        check_targets(admin_on_dev, [DEV_URL])


def test_admin_on_dev_port_with_other_user_is_refused(no_connections: None) -> None:
    """The dev cluster's port is refused even with another user: a test run would reset dev roles."""
    admin_on_dev_port = "postgresql://postgres:other-secret@127.0.0.1:5432/postgres"
    with pytest.raises(GuardError, match="same host and port as a dev URL"):
        check_targets(admin_on_dev_port, [DEV_URL])


def test_admin_on_dev_host_via_localhost_is_refused(no_connections: None) -> None:
    """localhost and 127.0.0.1 reach the same server, so the name spelling does not get around it."""
    admin_on_dev_host = "postgresql://postgres:other-secret@localhost:5432/postgres"
    with pytest.raises(GuardError, match="same host and port as a dev URL"):
        check_targets(admin_on_dev_host, [DEV_URL])


def test_admin_on_test_cluster_port_is_allowed(no_connections: None) -> None:
    """The dedicated test cluster (port 5433) passes next to a dev URL on 5432."""
    admin_on_test_cluster = "postgresql://postgres:test-secret@127.0.0.1:5433/postgres"
    check_targets(admin_on_test_cluster, [DEV_URL])


def test_endpoint_normalises_localhost() -> None:
    assert endpoint("postgresql://u:p@localhost:5432/x") == ("127.0.0.1", 5432)
    assert endpoint("postgresql://u:p@127.0.0.1:5433/x") == ("127.0.0.1", 5433)


def test_admin_url_on_test_database_is_refused(no_connections: None) -> None:
    """Even the test database is refused as the admin target: admin must use the maintenance DB."""
    admin_on_test = f"postgresql://postgres:admin-secret@127.0.0.1:5432/{TEST_DB_NAME}"
    with pytest.raises(GuardError, match="must connect to 'postgres'"):
        check_targets(admin_on_test, [DEV_URL])


def test_admin_url_equal_to_dev_url_is_refused(no_connections: None) -> None:
    """A dev URL reused as the admin URL is refused, even if its database name were otherwise fine."""
    same = "postgresql://postgres:admin-secret@127.0.0.1:5432/postgres"
    with pytest.raises(GuardError, match="same as a dev URL"):
        check_targets(same, [same])


def test_dev_url_naming_test_database_is_refused(no_connections: None) -> None:
    """A dev URL that names the test database would make the test run a dev run: refused."""
    dev_on_test = f"postgresql://app_user:x@127.0.0.1:5432/{TEST_DB_NAME}"
    with pytest.raises(GuardError, match="names the test database"):
        check_targets(ADMIN_OK, [dev_on_test])


def test_test_target_constant_is_a_test_name(no_connections: None) -> None:
    """Regression: the drop target must stay a *_test name, never a dev name."""
    assert TEST_DB_NAME == "grounded_answers_test"
    assert TEST_DB_NAME.endswith("_test")


def test_database_name_parses_scheme_variants() -> None:
    """Dev URLs use postgresql+psycopg://, tests use postgresql://. Both parse the same way."""
    assert database_name(DEV_URL) == "grounded_answers"
    assert database_name(ADMIN_OK) == "postgres"


def test_scratch_names_are_accepted(no_connections: None) -> None:
    """Both scratch databases may be created and dropped."""
    check_scratch_database(TEST_DB_NAME)
    check_scratch_database(ROUNDTRIP_DB_NAME)


@pytest.mark.parametrize("name", ["grounded_answers", "postgres", "grounded_answers_test_x"])
def test_any_other_name_is_refused(no_connections: None, name: str) -> None:
    """The dev database, the maintenance database and look-alike names are never dropped."""
    with pytest.raises(GuardError, match="not a scratch test database"):
        check_scratch_database(name)


def test_the_dev_port_is_refused_even_with_no_dev_url_configured(no_connections: None) -> None:
    """The bug of 2026-10-23: with no dev URL in the environment, an admin URL on the dev port passed the guard, and the
    fixture reset the dev cluster's roles. The dev port is refused on its own now."""
    admin_on_dev_port = "postgresql://postgres:other-secret@127.0.0.1:5432/postgres"
    with pytest.raises(GuardError, match="uses port 5432, the dev cluster"):
        check_targets(admin_on_dev_port, [])


def test_the_same_cluster_passes(no_connections: None) -> None:
    check_same_cluster("7699370521932238886", "7699370521932238886")


def test_a_psql_command_on_another_cluster_is_refused(no_connections: None) -> None:
    """PSQL_COMMAND named the dev container while the admin URL named the test cluster: refuse before any change."""
    with pytest.raises(GuardError, match="different PostgreSQL cluster"):
        check_same_cluster("7699370521932238886", "7699191651001303078")


def test_an_unknown_cluster_identity_is_refused(no_connections: None) -> None:
    with pytest.raises(GuardError, match="could not read the cluster identity"):
        check_same_cluster("", "7699370521932238886")
    with pytest.raises(GuardError, match="could not read the cluster identity"):
        check_same_cluster("7699370521932238886", "")

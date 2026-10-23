"""
Catalog guard (ADR 0006 amendment, point 7). Reads PostgreSQL's own catalogs and fails when
the database layout drifts from the design. Runs on every DB test session.

Grants are compared with expected_grants.json (schema grounded) and expected_queue_grants.json
(schema queue, the job queue, ADR 0016). Changing either list is a reviewed change.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from .database import DbHandle

pytestmark = pytest.mark.db

EXPECTED_GRANTS_FILE = Path(__file__).with_name("expected_grants.json")
EXPECTED_QUEUE_GRANTS_FILE = Path(__file__).with_name("expected_queue_grants.json")

LOGIN_ATTRS = {
    "rolsuper": False,
    "rolbypassrls": False,
    "rolcreatedb": False,
    "rolcreaterole": False,
    "rolinherit": False,
}
DEFINER_ATTRS = {"rolcanlogin": False, "rolsuper": False, "rolbypassrls": False}


@pytest.fixture
def catalog(grounded_db: DbHandle) -> Iterator[psycopg.Connection]:
    """Cluster-admin connection to the test database, for catalog reads."""
    conn = psycopg.connect(grounded_db.admin_test_url())
    yield conn
    conn.close()


def _role_attrs(conn: psycopg.Connection, role: str) -> dict[str, bool]:
    row = conn.execute(
        "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolinherit, rolcanlogin "
        "FROM pg_roles WHERE rolname = %s",
        (role,),
    ).fetchone()
    assert row is not None, f"role {role} does not exist"
    names = (
        "rolsuper",
        "rolbypassrls",
        "rolcreatedb",
        "rolcreaterole",
        "rolinherit",
        "rolcanlogin",
    )
    return dict(zip(names, row, strict=True))


@pytest.mark.parametrize("role", ["migration_role", "app_user", "worker_user"])
def test_login_roles_have_no_superuser_or_bypassrls(catalog: psycopg.Connection, role: str) -> None:
    """Login roles are never superuser, never BYPASSRLS, never NOINHERIT-violating."""
    attrs = _role_attrs(catalog, role)
    for name, expected in LOGIN_ATTRS.items():
        assert attrs[name] is expected, f"{role}.{name} should be {expected}"
    assert attrs["rolcanlogin"] is True


@pytest.mark.parametrize("role", ["auth_definer", "retention_definer", "provisioning_definer"])
def test_definer_roles_are_nologin_and_not_bypassrls(
    catalog: psycopg.Connection, role: str
) -> None:
    attrs = _role_attrs(catalog, role)
    for name, expected in DEFINER_ATTRS.items():
        assert attrs[name] is expected, f"{role}.{name} should be {expected}"


def test_public_has_no_connect_temp_or_create(catalog: psycopg.Connection) -> None:
    """PUBLIC (grantee oid 0) holds no CONNECT or TEMP on the database and no CREATE on public."""
    db_public = catalog.execute(
        "SELECT a.privilege_type FROM pg_database d, aclexplode(d.datacl) a "
        "WHERE d.datname = current_database() AND a.grantee = 0"
    ).fetchall()
    assert db_public == [], f"PUBLIC has database privileges: {db_public}"

    schema_public = catalog.execute(
        "SELECT a.privilege_type FROM pg_namespace n, aclexplode(n.nspacl) a "
        "WHERE n.nspname = 'public' AND a.grantee = 0"
    ).fetchall()
    assert schema_public == [], f"PUBLIC has privileges on schema public: {schema_public}"


def test_grounded_schema_is_owned_by_migration_role(catalog: psycopg.Connection) -> None:
    owner = catalog.execute(
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'grounded'"
    ).fetchone()
    assert owner == ("migration_role",)


def test_queue_schema_is_owned_by_migration_role(catalog: psycopg.Connection) -> None:
    owner = catalog.execute(
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'queue'"
    ).fetchone()
    assert owner == ("migration_role",)


# alembic_version is created by Alembic itself, owned by migration_role, and has no grants to
# any app role (test_grants_on_grounded_match_expected_list enforces that). FORCE RLS on it
# would stop Alembic from writing its own version row, so it is the one exemption.
RLS_EXEMPT_TABLES = frozenset({"alembic_version"})


def test_every_table_in_grounded_has_rls_enabled_and_forced(catalog: psycopg.Connection) -> None:
    """Every table except RLS_EXEMPT_TABLES has RLS enabled and forced."""
    rows = catalog.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'grounded' AND c.relkind IN ('r', 'p')"
    ).fetchall()
    not_enforced = [
        name
        for name, enabled, forced in rows
        if name not in RLS_EXEMPT_TABLES and not (enabled and forced)
    ]
    assert not_enforced == [], f"tables without RLS enabled and forced: {not_enforced}"


def test_security_definer_functions_pin_search_path_and_are_not_public(
    catalog: psycopg.Connection,
) -> None:
    """Every SECURITY DEFINER function pins search_path and has no EXECUTE for PUBLIC."""
    rows = catalog.execute(
        "SELECT p.proname, p.proconfig, "
        "  EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
        "          WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'grounded' AND p.prosecdef"
    ).fetchall()
    problems = []
    for name, config, public_execute in rows:
        if not config or not any(item.startswith("search_path=") for item in config):
            problems.append(f"{name}: search_path not pinned")
        if public_execute:
            problems.append(f"{name}: EXECUTE granted to PUBLIC")
    assert problems == []


def _actual_grants(conn: psycopg.Connection, schema: str) -> list[str]:
    """
    Every grant on objects in one schema to a role other than the owner, as stable strings.

    Excludes each object's owner (its own rights are not grants). Reads the ACLs directly
    (pg_class, pg_attribute, pg_proc, pg_type, pg_namespace). The
    information_schema column view also lists columns implied by table grants, so it
    would make the expected list unreadable. coalesce(..., acldefault(...)) makes the
    defaults visible too: a default that grants EXECUTE or USAGE to PUBLIC shows up as a grant.
    Types: only enums and standalone composite types. A table's row type cannot be granted.
    """
    found: list[str] = []

    for grantee, name, kind, privilege in conn.execute(
        """
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               c.relname,
               CASE c.relkind WHEN 'S' THEN 'sequence' ELSE 'table' END,
               a.privilege_type
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
             aclexplode(coalesce(c.relacl,
                                 acldefault((CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END)::"char",
                                            c.relowner))) a
        WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'S')
          AND a.grantee <> c.relowner
        """,
        (schema,),
    ).fetchall():
        found.append(f"{grantee}|{kind}:{name}|{privilege}")

    for grantee, table, column, privilege in conn.execute(
        """
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               c.relname, att.attname, a.privilege_type
        FROM pg_attribute att JOIN pg_class c ON c.oid = att.attrelid
             JOIN pg_namespace n ON n.oid = c.relnamespace,
             aclexplode(att.attacl) a
        WHERE n.nspname = %s AND att.attnum > 0 AND NOT att.attisdropped
          AND a.grantee <> c.relowner
        """,
        (schema,),
    ).fetchall():
        found.append(f"{grantee}|column:{table}.{column}|{privilege}")

    for grantee, name, privilege in conn.execute(
        """
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               p.proname, a.privilege_type
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace,
             aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
        WHERE n.nspname = %s
          AND a.grantee <> p.proowner
        """,
        (schema,),
    ).fetchall():
        found.append(f"{grantee}|routine:{name}|{privilege}")

    for grantee, name, privilege in conn.execute(
        """
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               t.typname, a.privilege_type
        FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
             LEFT JOIN pg_class rel ON rel.oid = t.typrelid,
             aclexplode(coalesce(t.typacl, acldefault('T', t.typowner))) a
        WHERE n.nspname = %s
          AND (t.typtype = 'e' OR (t.typtype = 'c' AND rel.relkind = 'c'))
          AND a.grantee <> t.typowner
        """,
        (schema,),
    ).fetchall():
        found.append(f"{grantee}|type:{name}|{privilege}")

    for grantee, privilege in conn.execute(
        """
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               a.privilege_type
        FROM pg_namespace n,
             aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
        WHERE n.nspname = %s
          AND a.grantee <> n.nspowner
        """,
        (schema,),
    ).fetchall():
        found.append(f"{grantee}|schema:{schema}|{privilege}")

    return sorted(found)


def test_grants_on_grounded_match_expected_list(catalog: psycopg.Connection) -> None:
    """Any grant not in expected_grants.json fails. Changing the list is a reviewed change."""
    expected = sorted(json.loads(EXPECTED_GRANTS_FILE.read_text(encoding="utf-8")))
    actual = _actual_grants(catalog, "grounded")
    assert actual == expected, (
        f"unexpected grants:\n  extra: {set(actual) - set(expected)}\n"
        f"  missing: {set(expected) - set(actual)}"
    )


def test_queue_has_no_public_privileges(catalog: psycopg.Connection) -> None:
    """PUBLIC keeps no privilege on any queue object: no EXECUTE, USAGE, SELECT or others."""
    public = [grant for grant in _actual_grants(catalog, "queue") if grant.startswith("PUBLIC|")]
    assert public == [], f"PUBLIC has privileges on queue objects: {public}"


def test_grants_on_queue_match_expected_list(catalog: psycopg.Connection) -> None:
    """Any grant on the queue schema not in expected_queue_grants.json fails (ADR 0016)."""
    expected = sorted(json.loads(EXPECTED_QUEUE_GRANTS_FILE.read_text(encoding="utf-8")))
    actual = _actual_grants(catalog, "queue")
    assert actual == expected, (
        f"unexpected queue grants:\n  extra: {set(actual) - set(expected)}\n"
        f"  missing: {set(expected) - set(actual)}"
    )


def test_extensions_schema_has_no_public_privileges(catalog: psycopg.Connection) -> None:
    """PUBLIC (grantee oid 0) holds nothing on the extensions schema (ADR 0021, bootstrap step 3c)."""
    public = catalog.execute(
        "SELECT a.privilege_type FROM pg_namespace n, aclexplode(n.nspacl) a "
        "WHERE n.nspname = 'extensions' AND a.grantee = 0"
    ).fetchall()
    assert public == [], f"PUBLIC has privileges on schema extensions: {public}"


def test_extensions_schema_grants_usage_to_the_login_roles_only(
    catalog: psycopg.Connection,
) -> None:
    """Only the three login roles hold USAGE, and nobody but the owner holds CREATE. The owner, the
    admin who runs the bootstrap, is excluded."""
    rows = catalog.execute(
        "SELECT r.rolname, a.privilege_type FROM pg_namespace n, aclexplode(n.nspacl) a "
        "JOIN pg_roles r ON r.oid = a.grantee "
        "WHERE n.nspname = 'extensions' AND a.grantee <> n.nspowner "
        "ORDER BY r.rolname, a.privilege_type"
    ).fetchall()
    assert rows == [
        ("app_user", "USAGE"),
        ("migration_role", "USAGE"),
        ("worker_user", "USAGE"),
    ], f"unexpected privileges on schema extensions: {rows}"


def test_vector_extension_is_installed_in_the_extensions_schema(
    catalog: psycopg.Connection,
) -> None:
    row = catalog.execute(
        "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
        "WHERE e.extname = 'vector'"
    ).fetchone()
    assert row == ("extensions",), f"pgvector is not in schema extensions: {row}"

"""Tenant allowlist, RLS policies, SECURITY DEFINER functions, and definer grants.

Revision ID: 0003_policies_and_definers
Revises: 0002_roles_and_grants
Create Date: 2026-10-22

Sources: ADR 0004 (login, refresh, definers, provisioning), ADR 0005 (tenant context),
ADR 0007 (worker), ADR 0006 (catalog guard). Every policy is explicit SQL.

Employee creation (decision B, ADR 0004): app_user has no INSERT on users or
tenant_memberships. Both rows are created only by create_tenant_user, a narrow SECURITY
DEFINER function owned by provisioning_definer. It takes the tenant from the session,
checks that the actor is an active tenant_admin of that tenant, and checks the email's
domain against tenant_email_domains.

Runs as migration_role.
"""

from collections.abc import Sequence

from alembic import op

revision = "0003_policies_and_definers"
down_revision = "0002_roles_and_grants"
branch_labels = None
depends_on = None

# The tenant predicate. With no tenant set, current_setting(..., true) is NULL: no row matches.
CUR = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"

# app_user's tenant-scoped commands. Every INSERT here is WITH CHECK (tenant predicate).
# No INSERT on tenant_memberships (only create_tenant_user creates memberships).
APP_TENANT_TABLES: Sequence[tuple[str, Sequence[str]]] = (
    ("tenant_memberships", ("SELECT", "UPDATE")),
    ("documents", ("SELECT", "INSERT", "UPDATE")),
    ("document_versions", ("SELECT", "INSERT", "UPDATE")),
    ("audit_log", ("SELECT", "INSERT")),
    ("refresh_tokens", ("SELECT", "INSERT", "UPDATE")),
    ("password_reset_tokens", ("SELECT", "INSERT", "UPDATE")),
    ("tenant_email_domains", ("SELECT",)),
)

# Operator access (ADR 0004): migration_role sees and writes every table.
OPERATOR_TABLES: Sequence[str] = (
    "tenants",
    "users",
    "tenant_memberships",
    "documents",
    "document_versions",
    "audit_log",
    "refresh_tokens",
    "password_reset_tokens",
    "tenant_email_domains",
)

CREATE_TENANT_USER_SIG = "grounded.create_tenant_user(text, text, text)"

_UP_TEMPLATES: Sequence[str] = (
    # ---- tenant allowlist: managed only by migration_role / the admin CLI (ADR 0004) ----
    """
    CREATE TABLE grounded.tenant_email_domains (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL
        REFERENCES grounded.tenants (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      domain text NOT NULL CHECK (
        domain = lower(domain) AND domain ~ '^[a-z0-9-]+(\\.[a-z0-9-]+)+$'
        AND char_length(domain) BETWEEN 4 AND 253
      ),
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT tenant_email_domains_tenant_id_id_key UNIQUE (tenant_id, id),
      CONSTRAINT tenant_email_domains_tenant_domain_key UNIQUE (tenant_id, domain)
    )
    """,
    "ALTER TABLE grounded.tenant_email_domains ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.tenant_email_domains FORCE ROW LEVEL SECURITY",
    # ---- app_user: tenant-scoped, USING and WITH CHECK ----
    "CREATE POLICY app_user_tenants_select ON grounded.tenants FOR SELECT TO app_user "
    "USING (id = <TENANT>)",
    # users: visible and writable only through a membership in the current tenant.
    # No INSERT policy: employee creation goes through create_tenant_user.
    "CREATE POLICY app_user_users_select ON grounded.users FOR SELECT TO app_user USING ("
    "EXISTS (SELECT 1 FROM grounded.tenant_memberships AS m "
    "WHERE m.user_id = users.id AND m.tenant_id = <TENANT>))",
    "CREATE POLICY app_user_users_update ON grounded.users FOR UPDATE TO app_user "
    "USING (EXISTS (SELECT 1 FROM grounded.tenant_memberships AS m "
    "WHERE m.user_id = users.id AND m.tenant_id = <TENANT>)) "
    "WITH CHECK (EXISTS (SELECT 1 FROM grounded.tenant_memberships AS m "
    "WHERE m.user_id = users.id AND m.tenant_id = <TENANT>))",
    *[
        stmt
        for table, commands in APP_TENANT_TABLES
        for stmt in (
            (
                f"CREATE POLICY app_user_{table}_select ON grounded.{table} "
                f"FOR SELECT TO app_user USING (tenant_id = <TENANT>)"
            )
            if "SELECT" in commands
            else "",
            (
                f"CREATE POLICY app_user_{table}_insert ON grounded.{table} "
                f"FOR INSERT TO app_user WITH CHECK (tenant_id = <TENANT>)"
            )
            if "INSERT" in commands
            else "",
            (
                f"CREATE POLICY app_user_{table}_update ON grounded.{table} "
                f"FOR UPDATE TO app_user USING (tenant_id = <TENANT>) "
                f"WITH CHECK (tenant_id = <TENANT>)"
            )
            if "UPDATE" in commands
            else "",
        )
        if stmt
    ],
    # ---- worker_user (ADR 0007): only the purge steps, each narrowed by state ----
    "CREATE POLICY worker_documents_select ON grounded.documents FOR SELECT TO worker_user "
    "USING (tenant_id = <TENANT>)",
    # Step 2 of the purge: clear current_version_id on a deleted document, nothing else.
    "CREATE POLICY worker_documents_update ON grounded.documents FOR UPDATE TO worker_user "
    "USING (tenant_id = <TENANT> AND deleted_at IS NOT NULL) "
    "WITH CHECK (tenant_id = <TENANT> AND deleted_at IS NOT NULL AND current_version_id IS NULL)",
    # Step 5 of the purge: a deleted document with no versions left.
    "CREATE POLICY worker_documents_delete ON grounded.documents FOR DELETE TO worker_user "
    "USING (tenant_id = <TENANT> AND deleted_at IS NOT NULL AND NOT EXISTS ("
    "SELECT 1 FROM grounded.document_versions AS v WHERE v.document_id = documents.id))",
    "CREATE POLICY worker_versions_select ON grounded.document_versions FOR SELECT TO worker_user "
    "USING (tenant_id = <TENANT>)",
    # Step 1 of the purge: retired -> purging, and nothing else.
    "CREATE POLICY worker_versions_update ON grounded.document_versions FOR UPDATE TO worker_user "
    "USING (tenant_id = <TENANT> AND status = 'retired') "
    "WITH CHECK (tenant_id = <TENANT> AND status = 'purging')",
    # Step 6 of the purge: only rows already in purging.
    "CREATE POLICY worker_versions_delete ON grounded.document_versions FOR DELETE TO worker_user "
    "USING (tenant_id = <TENANT> AND status = 'purging')",
    "CREATE POLICY worker_audit_insert ON grounded.audit_log FOR INSERT TO worker_user "
    "WITH CHECK (tenant_id = <TENANT>)",
    # ---- migration_role (operator, ADR 0004): explicit, visible, not a bypass ----
    *[
        f"CREATE POLICY migration_role_{table}_all ON grounded.{table} FOR ALL TO migration_role "
        "USING (true) WITH CHECK (true)"
        for table in OPERATOR_TABLES
    ],
    # ---- auth_definer (ADR 0004): read-only, for the login and refresh functions ----
    "CREATE POLICY auth_definer_users_select ON grounded.users FOR SELECT TO auth_definer "
    "USING (true)",
    "CREATE POLICY auth_definer_memberships_select ON grounded.tenant_memberships "
    "FOR SELECT TO auth_definer USING (true)",
    "CREATE POLICY auth_definer_tenants_select ON grounded.tenants FOR SELECT TO auth_definer "
    "USING (true)",
    "CREATE POLICY auth_definer_refresh_select ON grounded.refresh_tokens "
    "FOR SELECT TO auth_definer USING (true)",
    # ---- retention_definer (ADR 0007): read-only, for the due-tenant function ----
    "CREATE POLICY retention_definer_documents_select ON grounded.documents "
    "FOR SELECT TO retention_definer USING (true)",
    "CREATE POLICY retention_definer_versions_select ON grounded.document_versions "
    "FOR SELECT TO retention_definer USING (true)",
    # ---- provisioning_definer (ADR 0004 decision B): scoped to the session tenant ----
    "CREATE POLICY provisioning_memberships_select ON grounded.tenant_memberships "
    "FOR SELECT TO provisioning_definer USING (tenant_id = <TENANT>)",
    "CREATE POLICY provisioning_memberships_insert ON grounded.tenant_memberships "
    "FOR INSERT TO provisioning_definer WITH CHECK (tenant_id = <TENANT>)",
    "CREATE POLICY provisioning_domains_select ON grounded.tenant_email_domains "
    "FOR SELECT TO provisioning_definer USING (tenant_id = <TENANT>)",
    # users has no tenant column. The function's membership insert, in the same transaction,
    # is the gate. A user row without a membership cannot log in (ADR 0004).
    "CREATE POLICY provisioning_users_insert ON grounded.users FOR INSERT TO provisioning_definer "
    "WITH CHECK (true)",
    # ---- Definer schema and column grants: exactly what the function bodies read ----
    "GRANT SELECT ON grounded.tenant_email_domains TO app_user",
    "GRANT USAGE ON SCHEMA grounded TO auth_definer, retention_definer, provisioning_definer",
    "GRANT SELECT (id, email, password_hash, active) ON grounded.users TO auth_definer",
    "GRANT SELECT (tenant_id, user_id, role, active) ON grounded.tenant_memberships "
    "TO auth_definer",
    "GRANT SELECT (id, status) ON grounded.tenants TO auth_definer",
    "GRANT SELECT (id, tenant_id, user_id, family_id, token_hash, used_at, revoked_at, expires_at) "
    "ON grounded.refresh_tokens TO auth_definer",
    "GRANT SELECT (tenant_id, deleted_at, purge_after) ON grounded.documents TO retention_definer",
    "GRANT SELECT (tenant_id, purge_after, status) ON grounded.document_versions "
    "TO retention_definer",
    "GRANT INSERT ON grounded.users TO provisioning_definer",
    "GRANT SELECT (tenant_id, user_id, role, active) ON grounded.tenant_memberships "
    "TO provisioning_definer",
    "GRANT INSERT ON grounded.tenant_memberships TO provisioning_definer",
    "GRANT SELECT (tenant_id, domain) ON grounded.tenant_email_domains TO provisioning_definer",
    # ---- SECURITY DEFINER functions, created by migration_role ----
    # login: email -> user, membership, tenant status. Returns the hash only so the app can
    # verify it with Argon2 (ADR 0004). Unknown email: zero rows.
    """
    CREATE FUNCTION grounded.auth_login_lookup(p_email text)
    RETURNS TABLE (
      user_id uuid,
      password_hash text,
      user_active boolean,
      tenant_id uuid,
      tenant_status text,
      membership_role text,
      membership_active boolean
    )
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, grounded
    AS $$
      SELECT u.id, u.password_hash, u.active,
             m.tenant_id, t.status, m.role, m.active
      FROM grounded.users AS u
      LEFT JOIN grounded.tenant_memberships AS m ON m.user_id = u.id
      LEFT JOIN grounded.tenants AS t ON t.id = m.tenant_id
      WHERE u.email = pg_catalog.lower(p_email)
    $$
    """,
    # refresh: opaque token hash -> row metadata. The hash itself is never returned.
    """
    CREATE FUNCTION grounded.auth_refresh_lookup(p_token_hash bytea)
    RETURNS TABLE (
      token_id uuid,
      user_id uuid,
      tenant_id uuid,
      family_id uuid,
      used_at timestamptz,
      revoked_at timestamptz,
      expires_at timestamptz
    )
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, grounded
    AS $$
      SELECT r.id, r.user_id, r.tenant_id, r.family_id, r.used_at, r.revoked_at, r.expires_at
      FROM grounded.refresh_tokens AS r
      WHERE r.token_hash = p_token_hash
    $$
    """,
    # retention: tenant ids with a purge due. Ids only, no document data (ADR 0007).
    """
    CREATE FUNCTION grounded.retention_due_tenant_ids() RETURNS SETOF uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, grounded
    AS $$
      SELECT DISTINCT d.tenant_id
      FROM grounded.documents AS d
      WHERE d.deleted_at IS NOT NULL AND d.purge_after <= pg_catalog.now()
      UNION
      SELECT v.tenant_id
      FROM grounded.document_versions AS v
      WHERE v.status IN ('retired', 'purging') AND v.purge_after <= pg_catalog.now()
    $$
    """,
    # create_tenant_user (ADR 0004, decision B). Every failure raises the same message, so
    # the caller cannot tell "no such tenant", "not an admin", "domain not allowed" and
    # "email already taken" apart. The actor and the tenant come from the session settings
    # app.current_user and app.current_tenant, set in the one auth code path. Never a
    # parameter. Defence in depth, not a hard boundary (ADR 0004).
    """
    CREATE FUNCTION grounded.create_tenant_user(
      p_email text,
      p_display_name text,
      p_role text
    ) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, grounded
    AS $$
    DECLARE
      v_tenant uuid := NULLIF(pg_catalog.current_setting('app.current_tenant', true), '')::uuid;
      v_actor uuid := NULLIF(pg_catalog.current_setting('app.current_user', true), '')::uuid;
      v_email text := pg_catalog.lower(pg_catalog.btrim(p_email));
      v_domain text;
      v_user uuid := pg_catalog.gen_random_uuid();
    BEGIN
      IF v_tenant IS NULL
         OR v_actor IS NULL
         OR p_role NOT IN ('employee', 'tenant_admin')
         OR p_display_name IS NULL
         OR pg_catalog.char_length(p_display_name) NOT BETWEEN 1 AND 120
         OR v_email !~ '^[^@[:space:]]{1,64}@[a-z0-9.-]{3,253}$' THEN
        RAISE EXCEPTION 'could not create this user' USING ERRCODE = 'P0001';
      END IF;

      v_domain := pg_catalog.split_part(v_email, '@', 2);

      -- The actor must be an active tenant_admin of THIS tenant.
      IF NOT EXISTS (
        SELECT 1 FROM grounded.tenant_memberships AS m
        WHERE m.user_id = v_actor
          AND m.tenant_id = v_tenant
          AND m.role = 'tenant_admin'
          AND m.active
      ) THEN
        RAISE EXCEPTION 'could not create this user' USING ERRCODE = 'P0001';
      END IF;

      -- The domain must be on this tenant's allowlist.
      IF NOT EXISTS (
        SELECT 1 FROM grounded.tenant_email_domains AS d
        WHERE d.tenant_id = v_tenant AND d.domain = v_domain
      ) THEN
        RAISE EXCEPTION 'could not create this user' USING ERRCODE = 'P0001';
      END IF;

      BEGIN
        INSERT INTO grounded.users (id, email, display_name)
        VALUES (v_user, v_email, p_display_name);
        INSERT INTO grounded.tenant_memberships (id, tenant_id, user_id, role)
        VALUES (pg_catalog.gen_random_uuid(), v_tenant, v_user, p_role);
      EXCEPTION WHEN unique_violation THEN
        RAISE EXCEPTION 'could not create this user' USING ERRCODE = 'P0001';
      END;

      RETURN v_user;
    END;
    $$
    """,
    # EXECUTE grants are made while migration_role still owns the functions. After the owner
    # handover, migration_role (INHERIT FALSE) has no rights on them, and PostgreSQL silently
    # ignores a REVOKE or GRANT from a non-owner (a WARNING, not an error). The ACL moves with
    # the owner, so the grants survive the handover.
    # ---- No PUBLIC EXECUTE; each caller gets exactly the function it needs ----
    "REVOKE ALL ON FUNCTION grounded.auth_login_lookup(text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION grounded.auth_login_lookup(text) TO app_user",
    "REVOKE ALL ON FUNCTION grounded.auth_refresh_lookup(bytea) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION grounded.auth_refresh_lookup(bytea) TO app_user",
    "REVOKE ALL ON FUNCTION grounded.retention_due_tenant_ids() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION grounded.retention_due_tenant_ids() TO worker_user",
    f"REVOKE ALL ON FUNCTION {CREATE_TENANT_USER_SIG} FROM PUBLIC",
    f"GRANT EXECUTE ON FUNCTION {CREATE_TENANT_USER_SIG} TO app_user",
    # ---- Owner handover (ADR 0006, point 5): CREATE on the schema only for the change ----
    "GRANT CREATE ON SCHEMA grounded TO auth_definer",
    "ALTER FUNCTION grounded.auth_login_lookup(text) OWNER TO auth_definer",
    "ALTER FUNCTION grounded.auth_refresh_lookup(bytea) OWNER TO auth_definer",
    "REVOKE CREATE ON SCHEMA grounded FROM auth_definer",
    "GRANT CREATE ON SCHEMA grounded TO retention_definer",
    "ALTER FUNCTION grounded.retention_due_tenant_ids() OWNER TO retention_definer",
    "REVOKE CREATE ON SCHEMA grounded FROM retention_definer",
    "GRANT CREATE ON SCHEMA grounded TO provisioning_definer",
    f"ALTER FUNCTION {CREATE_TENANT_USER_SIG} OWNER TO provisioning_definer",
    "REVOKE CREATE ON SCHEMA grounded FROM provisioning_definer",
)

# Resolve the tenant token once, when the module loads. The SQL is constant text.
UP: Sequence[str] = tuple(stmt.replace("<TENANT>", CUR) for stmt in _UP_TEMPLATES)

# Reverse order. A SECURITY DEFINER function is owned by its definer role, and migration_role
# has INHERIT FALSE membership, so it cannot drop those functions directly. It must SET ROLE to
# the owner first (ADR 0006, point 4). SET ROLE needs the SET TRUE membership.
DOWN: Sequence[str] = (
    "SET ROLE provisioning_definer",
    f"DROP FUNCTION {CREATE_TENANT_USER_SIG}",
    "RESET ROLE",
    "SET ROLE auth_definer",
    "DROP FUNCTION grounded.auth_refresh_lookup(bytea)",
    "DROP FUNCTION grounded.auth_login_lookup(text)",
    "RESET ROLE",
    "SET ROLE retention_definer",
    "DROP FUNCTION grounded.retention_due_tenant_ids()",
    "RESET ROLE",
    "REVOKE USAGE ON SCHEMA grounded FROM auth_definer, retention_definer, provisioning_definer",
    "REVOKE SELECT (tenant_id, domain) ON grounded.tenant_email_domains FROM provisioning_definer",
    "REVOKE INSERT ON grounded.tenant_memberships FROM provisioning_definer",
    "REVOKE SELECT (tenant_id, user_id, role, active) ON grounded.tenant_memberships "
    "FROM provisioning_definer",
    "REVOKE INSERT ON grounded.users FROM provisioning_definer",
    "REVOKE SELECT (tenant_id, purge_after, status) ON grounded.document_versions "
    "FROM retention_definer",
    "REVOKE SELECT (tenant_id, deleted_at, purge_after) ON grounded.documents FROM retention_definer",
    "REVOKE SELECT (id, tenant_id, user_id, family_id, token_hash, used_at, revoked_at, expires_at) "
    "ON grounded.refresh_tokens FROM auth_definer",
    "REVOKE SELECT (id, status) ON grounded.tenants FROM auth_definer",
    "REVOKE SELECT (tenant_id, user_id, role, active) ON grounded.tenant_memberships "
    "FROM auth_definer",
    "REVOKE SELECT (id, email, password_hash, active) ON grounded.users FROM auth_definer",
    "DROP POLICY provisioning_users_insert ON grounded.users",
    "DROP POLICY provisioning_domains_select ON grounded.tenant_email_domains",
    "DROP POLICY provisioning_memberships_insert ON grounded.tenant_memberships",
    "DROP POLICY provisioning_memberships_select ON grounded.tenant_memberships",
    "DROP POLICY retention_definer_versions_select ON grounded.document_versions",
    "DROP POLICY retention_definer_documents_select ON grounded.documents",
    "DROP POLICY auth_definer_refresh_select ON grounded.refresh_tokens",
    "DROP POLICY auth_definer_tenants_select ON grounded.tenants",
    "DROP POLICY auth_definer_memberships_select ON grounded.tenant_memberships",
    "DROP POLICY auth_definer_users_select ON grounded.users",
    *[
        f"DROP POLICY migration_role_{table}_all ON grounded.{table}"
        for table in reversed(OPERATOR_TABLES)
    ],
    "DROP POLICY worker_audit_insert ON grounded.audit_log",
    "DROP POLICY worker_versions_delete ON grounded.document_versions",
    "DROP POLICY worker_versions_update ON grounded.document_versions",
    "DROP POLICY worker_versions_select ON grounded.document_versions",
    "DROP POLICY worker_documents_delete ON grounded.documents",
    "DROP POLICY worker_documents_update ON grounded.documents",
    "DROP POLICY worker_documents_select ON grounded.documents",
    *[
        f"DROP POLICY app_user_{table}_{command.lower()} ON grounded.{table}"
        for table, commands in reversed(APP_TENANT_TABLES)
        for command in reversed(commands)
    ],
    "DROP POLICY app_user_users_update ON grounded.users",
    "DROP POLICY app_user_users_select ON grounded.users",
    "DROP POLICY app_user_tenants_select ON grounded.tenants",
    "DROP TABLE grounded.tenant_email_domains",
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

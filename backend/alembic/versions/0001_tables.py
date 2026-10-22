"""Create the grounded tables, constraints, updated_at trigger, and RLS enable+force.

Revision ID: 0001_tables
Revises:
Create Date: 2026-10-22

Source of truth: docs/ARCHITECTURE.md section 3 (table definitions) and section 4 (ON DELETE).
Every foreign key is ON DELETE RESTRICT and ON UPDATE RESTRICT.
Every tenant-owned table has PRIMARY KEY (id), UNIQUE (tenant_id, id), tenant_id NOT NULL.

RLS is ENABLED and FORCED on every table here, but there are NO policies yet (ADR 0006,
Step 5 adds them). With no policy, every row is invisible to every role except the owner
(which is also forced to obey RLS). That is the fail-closed default.

Runs as migration_role (the owner of schema grounded). Never as app_user or worker_user.
"""

from collections.abc import Sequence

from alembic import op

revision = "0001_tables"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "grounded"

# Statements run in order, one per op.execute, so an error names the statement that failed.
UP: Sequence[str] = (
    # --- tenants (the tenant itself; not tenant-owned) ---
    """
    CREATE TABLE grounded.tenants (
      id uuid PRIMARY KEY,
      name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
      slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{3,40}$'),
      status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # --- users (global identity; not tenant-owned) ---
    """
    CREATE TABLE grounded.users (
      id uuid PRIMARY KEY,
      email text NOT NULL UNIQUE
        CHECK (email = lower(email) AND char_length(email) BETWEEN 3 AND 254),
      display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 120),
      password_hash text NULL,
      active boolean NOT NULL DEFAULT true,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # --- tenant_memberships ---
    """
    CREATE TABLE grounded.tenant_memberships (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL
        REFERENCES grounded.tenants (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      user_id uuid NOT NULL
        REFERENCES grounded.users (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      role text NOT NULL CHECK (role IN ('tenant_admin', 'employee')),
      active boolean NOT NULL DEFAULT true,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT tenant_memberships_tenant_id_id_key UNIQUE (tenant_id, id),
      CONSTRAINT tenant_memberships_user_id_key UNIQUE (user_id),
      CONSTRAINT tenant_memberships_tenant_id_user_id_key UNIQUE (tenant_id, user_id)
    )
    """,
    # --- documents (metadata; current_version_id FK added after document_versions) ---
    """
    CREATE TABLE grounded.documents (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL
        REFERENCES grounded.tenants (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
      current_version_id uuid NULL,
      deleted_at timestamptz NULL,
      purge_after timestamptz NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT documents_tenant_id_id_key UNIQUE (tenant_id, id),
      CONSTRAINT documents_deleted_purge_chk
        CHECK ((deleted_at IS NULL) = (purge_after IS NULL))
    )
    """,
    # --- document_versions ---
    """
    CREATE TABLE grounded.document_versions (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL
        REFERENCES grounded.tenants (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      document_id uuid NOT NULL,
      version_no integer NOT NULL CHECK (version_no >= 1),
      original_filename text NOT NULL
        CHECK (char_length(original_filename) BETWEEN 1 AND 255),
      content_type text NOT NULL CHECK (content_type IN (
        'application/pdf',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'text/plain',
        'text/markdown'
      )),
      size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 1 AND 10485760),
      sha256 bytea NOT NULL CHECK (octet_length(sha256) = 32),
      storage_key text NOT NULL UNIQUE
        CHECK (storage_key LIKE 'tenant/' || tenant_id::text || '/%'),
      status text NOT NULL CHECK (status IN (
        'queued', 'processing', 'ready', 'failed', 'retired', 'purging'
      )),
      failure_reason text NULL CHECK (char_length(failure_reason) <= 500),
      created_by uuid NOT NULL,
      retired_at timestamptz NULL,
      purge_after timestamptz NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT document_versions_tenant_id_id_key UNIQUE (tenant_id, id),
      CONSTRAINT document_versions_document_id_version_no_key UNIQUE (document_id, version_no),
      CONSTRAINT document_versions_document_fk
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES grounded.documents (tenant_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
      CONSTRAINT document_versions_created_by_fk
        FOREIGN KEY (tenant_id, created_by)
        REFERENCES grounded.tenant_memberships (tenant_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
      CONSTRAINT document_versions_retired_purge_chk
        CHECK ((retired_at IS NULL) = (purge_after IS NULL)),
      CONSTRAINT document_versions_status_retired_chk
        CHECK ((status IN ('retired', 'purging')) = (retired_at IS NOT NULL))
    )
    """,
    # Circular reference: documents -> current version. Deferred, NULL skips the check.
    """
    ALTER TABLE grounded.documents
      ADD CONSTRAINT documents_current_version_fk
      FOREIGN KEY (tenant_id, current_version_id)
      REFERENCES grounded.document_versions (tenant_id, id)
      ON DELETE RESTRICT ON UPDATE RESTRICT
      DEFERRABLE INITIALLY DEFERRED
    """,
    # Dedupe lookups are per tenant only (ARCHITECTURE 5.4). Not unique.
    "CREATE INDEX document_versions_tenant_sha256_idx "
    "ON grounded.document_versions (tenant_id, sha256)",
    "CREATE INDEX document_versions_tenant_document_idx "
    "ON grounded.document_versions (tenant_id, document_id)",
    # --- audit_log (append-only by grants, see 0002) ---
    """
    CREATE TABLE grounded.audit_log (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL
        REFERENCES grounded.tenants (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      actor_user_id uuid NULL,
      action text NOT NULL CHECK (char_length(action) BETWEEN 1 AND 64),
      target_type text NULL,
      target_id uuid NULL,
      ip_hmac bytea NULL,
      detail jsonb NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT audit_log_tenant_id_id_key UNIQUE (tenant_id, id),
      CONSTRAINT audit_log_actor_fk
        FOREIGN KEY (tenant_id, actor_user_id)
        REFERENCES grounded.tenant_memberships (tenant_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
    )
    """,
    "CREATE INDEX audit_log_tenant_created_idx ON grounded.audit_log (tenant_id, created_at)",
    # --- refresh_tokens (ADR 0004) ---
    """
    CREATE TABLE grounded.refresh_tokens (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL
        REFERENCES grounded.tenants (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      user_id uuid NOT NULL,
      family_id uuid NOT NULL,
      replaced_by uuid NULL,
      token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
      issued_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL,
      used_at timestamptz NULL,
      revoked_at timestamptz NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT refresh_tokens_tenant_id_id_key UNIQUE (tenant_id, id),
      CONSTRAINT refresh_tokens_user_fk
        FOREIGN KEY (tenant_id, user_id)
        REFERENCES grounded.tenant_memberships (tenant_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
      CONSTRAINT refresh_tokens_expiry_chk CHECK (expires_at > issued_at)
    )
    """,
    # Rotation chain. Added after the table so the self-reference is a plain ALTER.
    """
    ALTER TABLE grounded.refresh_tokens
      ADD CONSTRAINT refresh_tokens_replaced_by_fk
      FOREIGN KEY (tenant_id, replaced_by)
      REFERENCES grounded.refresh_tokens (tenant_id, id)
      ON DELETE RESTRICT ON UPDATE RESTRICT
    """,
    "CREATE INDEX refresh_tokens_user_id_idx ON grounded.refresh_tokens (user_id)",
    "CREATE INDEX refresh_tokens_family_id_idx ON grounded.refresh_tokens (family_id)",
    # --- password_reset_tokens (ADR 0004) ---
    """
    CREATE TABLE grounded.password_reset_tokens (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL
        REFERENCES grounded.tenants (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      user_id uuid NOT NULL,
      created_by uuid NOT NULL,
      token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
      expires_at timestamptz NOT NULL,
      used_at timestamptz NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT password_reset_tokens_tenant_id_id_key UNIQUE (tenant_id, id),
      CONSTRAINT password_reset_tokens_user_fk
        FOREIGN KEY (tenant_id, user_id)
        REFERENCES grounded.tenant_memberships (tenant_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
      CONSTRAINT password_reset_tokens_created_by_fk
        FOREIGN KEY (tenant_id, created_by)
        REFERENCES grounded.tenant_memberships (tenant_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
      CONSTRAINT password_reset_tokens_expiry_chk CHECK (expires_at > created_at)
    )
    """,
    "CREATE INDEX password_reset_tokens_user_id_idx ON grounded.password_reset_tokens (user_id)",
    # --- updated_at: one shared trigger function, search_path pinned, not SECURITY DEFINER ---
    """
    CREATE FUNCTION grounded.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, grounded
    AS $$
    BEGIN
      NEW.updated_at := now();
      RETURN NEW;
    END;
    $$
    """,
    # Functions are granted to PUBLIC by default. A trigger function is never called by a role,
    # so the default is removed. The catalog guard (test_catalog.py) enforces this.
    "REVOKE EXECUTE ON FUNCTION grounded.set_updated_at() FROM PUBLIC",
    "CREATE TRIGGER users_set_updated_at BEFORE UPDATE ON grounded.users "
    "FOR EACH ROW EXECUTE FUNCTION grounded.set_updated_at()",
    "CREATE TRIGGER tenant_memberships_set_updated_at BEFORE UPDATE ON grounded.tenant_memberships "
    "FOR EACH ROW EXECUTE FUNCTION grounded.set_updated_at()",
    "CREATE TRIGGER documents_set_updated_at BEFORE UPDATE ON grounded.documents "
    "FOR EACH ROW EXECUTE FUNCTION grounded.set_updated_at()",
    # --- RLS: enabled and forced on every table. No policies yet (Step 5): fail closed. ---
    "ALTER TABLE grounded.tenants ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.tenants FORCE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.users ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.users FORCE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.tenant_memberships ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.tenant_memberships FORCE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.documents ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.documents FORCE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.document_versions ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.document_versions FORCE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.audit_log ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.audit_log FORCE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.refresh_tokens ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.refresh_tokens FORCE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.password_reset_tokens ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.password_reset_tokens FORCE ROW LEVEL SECURITY",
)

# Reverse order of creation: children before parents (RESTRICT everywhere).
DOWN: Sequence[str] = (
    "DROP TABLE grounded.password_reset_tokens",
    "DROP TABLE grounded.refresh_tokens",
    "DROP TABLE grounded.audit_log",
    "ALTER TABLE grounded.documents DROP CONSTRAINT documents_current_version_fk",
    "DROP TABLE grounded.document_versions",
    "DROP TABLE grounded.documents",
    "DROP TABLE grounded.tenant_memberships",
    "DROP TABLE grounded.users",
    "DROP TABLE grounded.tenants",
    "DROP FUNCTION grounded.set_updated_at()",
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

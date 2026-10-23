"""Chunks: section-aware text pieces with an embedding and a full-text index (ADR 0021).

Revision ID: 0008_chunks
Revises: 0007_document_blobs
Create Date: 2026-10-23

One row per chunk of a document version. The chunk key is (tenant, version, position), so a retried
ingestion that finds partial chunks cannot create a second set (ingestion deletes the version's chunks
first; see the worker code and the crash-then-retry test).

The pgvector extension is installed by bootstrap_database.sql into the 'extensions' schema, not here.
Types and operators are qualified with extensions.* so the migration does not depend on search_path.

Policies (enforced by RLS, not by the application):
- app_user: SELECT only a chunk whose version is 'ready', is the document's current_version_id, and whose
  document is not deleted (ARCHITECTURE 5.6). Superseded, processing, failed, retired and deleted-document
  chunks are invisible.
- worker_user: SELECT and DELETE for chunks of 'processing' or 'purging' versions; INSERT only for
  'processing'. SELECT covers 'purging' and 'processing' because PostgreSQL requires a row to be visible
  through a SELECT policy before DELETE can remove it (ADR 0018 amendment; the Step 2a lesson).
- migration_role: all, as on the other tenant tables.

This is a new migration, not an edit of 0007, because 0007 is merged (CLAUDE.md, migrations rule).
"""

from collections.abc import Sequence

from alembic import op

revision = "0008_chunks"
down_revision = "0007_document_blobs"
branch_labels = None
depends_on = None

# The tenant predicate, as in 0003 and 0007: NULL when no tenant is set, so no row matches (fail closed).
CUR = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"

# The chunk's version, in the same tenant, with the status the policy allows.
WORKER_VERSION = (
    "EXISTS (SELECT 1 FROM grounded.document_versions AS v "
    "WHERE v.tenant_id = chunks.tenant_id AND v.id = chunks.version_id "
    "AND v.status {status})"
)

# The app may read a chunk only when its version is ready, is the document's current version, and the
# document is not deleted (ARCHITECTURE 5.6).
APP_READY_CURRENT = (
    "EXISTS (SELECT 1 FROM grounded.document_versions AS v "
    "JOIN grounded.documents AS d ON d.tenant_id = v.tenant_id AND d.id = v.document_id "
    "WHERE v.tenant_id = chunks.tenant_id AND v.id = chunks.version_id "
    "AND v.status = 'ready' AND d.current_version_id = v.id AND d.deleted_at IS NULL)"
)

UP: Sequence[str] = (
    """
    CREATE TABLE grounded.chunks (
      tenant_id uuid NOT NULL
        REFERENCES grounded.tenants (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      version_id uuid NOT NULL,
      position integer NOT NULL,
      section_number text NULL,
      heading text NOT NULL DEFAULT '',
      clause_labels text[] NOT NULL DEFAULT '{}',
      body text NOT NULL,
      embedding extensions.vector(384) NOT NULL,
      embedding_model text NOT NULL,
      embedding_model_version text NOT NULL,
      tsv tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, body)) STORED,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT chunks_pkey PRIMARY KEY (tenant_id, version_id, position),
      CONSTRAINT chunks_version_fk FOREIGN KEY (tenant_id, version_id)
        REFERENCES grounded.document_versions (tenant_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      CONSTRAINT chunks_position_check CHECK (position >= 0),
      CONSTRAINT chunks_body_check CHECK (char_length(body) BETWEEN 1 AND 20000),
      CONSTRAINT chunks_dims_check CHECK (extensions.vector_dims(embedding) = 384),
      CONSTRAINT chunks_model_check CHECK (
        char_length(embedding_model) BETWEEN 1 AND 200
        AND char_length(embedding_model_version) BETWEEN 1 AND 200)
    )
    """,
    "CREATE INDEX chunks_embedding_hnsw ON grounded.chunks "
    "USING hnsw (embedding extensions.vector_cosine_ops)",
    "CREATE INDEX chunks_tsv_gin ON grounded.chunks USING gin (tsv)",
    "ALTER TABLE grounded.chunks ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.chunks FORCE ROW LEVEL SECURITY",
    # ---- app_user: read ready, current, not-deleted chunks of its own tenant ----
    "CREATE POLICY app_user_chunks_select ON grounded.chunks "
    f"FOR SELECT TO app_user USING (tenant_id = {CUR} AND " + APP_READY_CURRENT + ")",
    # ---- worker_user: read, delete and insert chunks during ingestion and purge ----
    "CREATE POLICY worker_chunks_select ON grounded.chunks "
    f"FOR SELECT TO worker_user USING (tenant_id = {CUR} AND "
    + WORKER_VERSION.format(status="IN ('processing', 'purging')")
    + ")",
    "CREATE POLICY worker_chunks_insert ON grounded.chunks "
    f"FOR INSERT TO worker_user WITH CHECK (tenant_id = {CUR} AND "
    + WORKER_VERSION.format(status="= 'processing'")
    + ")",
    "CREATE POLICY worker_chunks_delete ON grounded.chunks "
    f"FOR DELETE TO worker_user USING (tenant_id = {CUR} AND "
    + WORKER_VERSION.format(status="IN ('processing', 'purging')")
    + ")",
    # ---- migration_role (operator, ADR 0004): explicit, as on the other tenant tables ----
    "CREATE POLICY migration_role_chunks_all ON grounded.chunks "
    "FOR ALL TO migration_role USING (true) WITH CHECK (true)",
    # ---- grants: the least that the policies above need ----
    "GRANT SELECT ON grounded.chunks TO app_user",
    "GRANT SELECT, INSERT, DELETE ON grounded.chunks TO worker_user",
)

DOWN: Sequence[str] = ("DROP TABLE grounded.chunks",)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

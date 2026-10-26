"""Drop the HNSW index on chunks.embedding (ADR 0030, amends ADR 0021 decision 3).

Revision ID: 0016_drop_hnsw_index
Revises: 0015_queue_cleanup
Create Date: 2026-10-26

Every retrieval query runs as app_user, so row-level security keeps one tenant's current chunks (ARCHITECTURE 5.6).
pgvector's HNSW index is approximate and filters after the index scan, so a filtered query can return fewer rows than
asked for, or the wrong nearest ones. The measurements are in docs/EVAL.md ("Retrieval strategy, Session 5 Step 2",
reproducible with `pytest -m eval -k scale`): the planner picks HNSW once a tenant has about 2,000 chunks, and from then
on the top 5 overlaps the exact top 5 by only 0.29 to 0.34, with queries returning as few as 2 of 50 rows. With
near-duplicate content in other tenants and default settings, every one of 50 queries returned fewer than 5 rows, and
no setting tried (larger ef_search, strict or relaxed iterative scans) fixed all of them.

Without the index, the planner narrows by the tenant-first primary key (tenant_id, version_id, position) and sorts the
tenant's rows by exact distance. That is correct at every size tested. Its cost grows with the tenant's own chunk
count, not the table's: p50 20 ms at 2,000 chunks and 188 ms at 20,000 (p95 369 ms). The thresholds for revisiting are
in ADR 0030 (retrieval p95 above about 250 ms, or a tenant above about 10,000 chunks).

The keyword index (chunks_tsv_gin) stays. This is a new migration, not an edit of 0008, because 0008 is applied
(CLAUDE.md, migrations rule). The downgrade recreates the index exactly as 0008 made it.
"""

from collections.abc import Sequence

from alembic import op

revision = "0016_drop_hnsw_index"
down_revision = "0015_queue_cleanup"
branch_labels = None
depends_on = None

UP: Sequence[str] = ("DROP INDEX grounded.chunks_embedding_hnsw",)

DOWN: Sequence[str] = (
    "CREATE INDEX chunks_embedding_hnsw ON grounded.chunks "
    "USING hnsw (embedding extensions.vector_cosine_ops)",
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)

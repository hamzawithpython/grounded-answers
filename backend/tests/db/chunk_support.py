"""
Chunks with chosen vectors, written as the cluster admin, for the retrieval tests and the scale test.

The retrieval tests need to know the vector order in advance, so they do not use the model: a vector is built from a
few weighted axes, which gives exact cosine similarities that can be reasoned about. Rows are committed, because
app_user must see them; each test uses its own fresh tenant (eval_support.fresh_tenant), so nothing is shared.
"""

import secrets
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
import psycopg
import pytest
from psycopg import sql

from grounded_answers.embedding.pinned import MODEL_NAME, MODEL_REVISION
from grounded_answers.models import EMBEDDING_DIMENSIONS

from .eval_support import EvalTenant, fresh_tenant


def axis_vector(weights: Mapping[int, float]) -> str:
    """A unit vector in pgvector's text form with the given weight on each listed axis."""
    values = np.zeros(EMBEDDING_DIMENSIONS)
    for axis, weight in weights.items():
        values[axis] = weight
    values /= np.linalg.norm(values)
    return vector_text(values)


def vector_text(values: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in values) + "]"


def as_floats(text_form: str) -> list[float]:
    return [float(x) for x in text_form.strip("[]").split(",")]


@dataclass(frozen=True)
class ChunkSpec:
    body: str
    vector: str
    section_number: str | None = "1"
    heading: str = "Heading"
    clause_labels: tuple[str, ...] = ("1.1",)


@dataclass(frozen=True)
class VersionSpec:
    """One version of a document. status is a document_versions status; current marks the document's current one."""

    chunks: Sequence[ChunkSpec]
    status: str = "ready"
    current: bool = True


@dataclass
class Inserted:
    document_id: uuid.UUID
    version_ids: list[uuid.UUID] = field(default_factory=list)


def insert_document(
    admin: psycopg.Connection,
    tenant: EvalTenant,
    title: str,
    versions: Sequence[VersionSpec],
    *,
    deleted: bool = False,
) -> Inserted:
    """Insert a document with its versions (oldest first) and their chunks. Several versions can be 'current'
    in the spec only if the caller wants that mistake; the document points at the last one marked current."""
    document = uuid.uuid4()
    now = datetime.now(UTC)
    admin.execute(
        "INSERT INTO grounded.documents (id, tenant_id, title) VALUES (%s, %s, %s)",
        (document, tenant.tenant, title),
    )
    inserted = Inserted(document)
    current_id: uuid.UUID | None = None
    for number, spec in enumerate(versions, start=1):
        version = uuid.uuid4()
        retired = spec.status in ("retired", "purging")
        admin.execute(
            """
            INSERT INTO grounded.document_versions (
              id, tenant_id, document_id, version_no, original_filename, content_type, size_bytes,
              sha256, storage_key, status, created_by, retired_at, purge_after,
              processing_started_at, processing_token
            ) VALUES (%s, %s, %s, %s, 'x.md', 'text/markdown', 1, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                version,
                tenant.tenant,
                document,
                number,
                secrets.token_bytes(32),
                f"tenant/{tenant.tenant}/{uuid.uuid4()}",
                spec.status,
                tenant.admin,
                now if retired else None,
                now + timedelta(days=30) if retired else None,
                now if spec.status == "processing" else None,
                uuid.uuid4() if spec.status == "processing" else None,
            ),
        )
        with admin.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO grounded.chunks (tenant_id, version_id, position, section_number, heading, "
                "clause_labels, body, embedding, embedding_model, embedding_model_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::extensions.vector, %s, %s)",
                [
                    (
                        tenant.tenant,
                        version,
                        position,
                        chunk.section_number,
                        chunk.heading,
                        list(chunk.clause_labels),
                        chunk.body,
                        chunk.vector,
                        MODEL_NAME,
                        MODEL_REVISION,
                    )
                    for position, chunk in enumerate(spec.chunks)
                ],
            )
        if spec.current:
            current_id = version
        inserted.version_ids.append(version)
    admin.execute(
        "UPDATE grounded.documents SET current_version_id = %s WHERE id = %s",
        (current_id, document),
    )
    if deleted:
        admin.execute(
            "UPDATE grounded.documents SET deleted_at = %s, purge_after = %s WHERE id = %s",
            (now, now + timedelta(days=30), document),
        )
    return inserted


def remove_tenant(admin: psycopg.Connection, tenant: EvalTenant) -> None:
    """
    Remove a tenant from fresh_tenant and everything it owns, in foreign-key order.

    Tests that commit rows must do this: the retrieval negative control runs as the cluster admin, which bypasses
    row-level security, so it sees every tenant's chunks, and a chunk left behind by an earlier test breaks it (found
    when the /ask tests left theirs: CI failed with UnknownVersionError, a local run in chunks did not).
    """
    admin.execute(
        "UPDATE grounded.documents SET current_version_id = NULL WHERE tenant_id = %s",
        (tenant.tenant,),
    )
    for table in (
        "answer_usage",
        "tenant_settings",
        "chunks",
        "audit_log",
        "document_versions",
        "documents",
    ):
        admin.execute(
            sql.SQL("DELETE FROM grounded.{} WHERE tenant_id = %s").format(sql.Identifier(table)),
            (tenant.tenant,),
        )
    admin.execute("DELETE FROM grounded.tenant_memberships WHERE tenant_id = %s", (tenant.tenant,))
    admin.execute(
        "DELETE FROM grounded.tenant_email_domains WHERE tenant_id = %s", (tenant.tenant,)
    )
    admin.execute(
        "DELETE FROM grounded.users WHERE id = ANY(%s)", ([tenant.admin, tenant.employee],)
    )
    admin.execute("DELETE FROM grounded.tenants WHERE id = %s", (tenant.tenant,))


@pytest.fixture
def tenants(admin: psycopg.Connection) -> Iterator[Callable[[], EvalTenant]]:
    """Makes tenants on demand and removes everything they own afterwards, in foreign-key order."""
    created: list[EvalTenant] = []

    def make() -> EvalTenant:
        tenant = fresh_tenant(admin)
        created.append(tenant)
        return tenant

    yield make
    for tenant in created:
        remove_tenant(admin, tenant)

"""
ORM models for the grounded schema (ADR 0005).

These mirror the tables the migrations create, and nothing else. Policies, grants, functions and
triggers are written in migrations as explicit SQL and tested there (ADR 0005). A test compares
these models with the migrated database (tests/db/test_orm_metadata.py), so the two cannot drift
apart without a failing test.

Rules that every model follows:
- Every constraint and index has the name the migration gave it. The drift test checks names.
- CHECK constraints are not declared here. tests/db/expected_checks.json holds the reviewed names
  and definitions instead (decision B1, ADR 0005 follow-up).
- Every relationship uses lazy="raise". Reading an unloaded relationship raises at once, so an
  async request can never fall back to a silent query (ADR 0005).
- Columns that app_user cannot SELECT (password_hash, updated_at on users) are deferred. A plain
  select(User) therefore does not name them, and the database does not refuse the query.
- created_at and similar server-side defaults are returned by INSERT (eager_defaults), so the
  app never reads an expired attribute, which would need a lazy load it cannot do in async code.
"""

import datetime
import uuid
from types import MappingProxyType
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import UserDefinedType

SCHEMA = "grounded"
# pgvector is installed here by bootstrap_database.sql (ADR 0021).
EXTENSIONS_SCHEMA = "extensions"
# multilingual-e5-small (ADR 0021). Migration 0008 and the vector type use the same number.
EMBEDDING_DIMENSIONS = 384

# Shorthand for the ON DELETE / ON UPDATE clause every foreign key in the migrations uses.
_RESTRICT: dict[str, Any] = {"ondelete": "RESTRICT", "onupdate": "RESTRICT"}


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)
    # Every INSERT returns server defaults (created_at, updated_at, active), so no attribute is
    # left expired after a flush (see the module docstring).
    __mapper_args__ = MappingProxyType({"eager_defaults": True})


def _uuid_pk() -> Mapped[uuid.UUID]:
    # ids are generated in the app with uuid4 (ADR 0005). The database has no default.
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _tenant_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.tenants.id", **_RESTRICT), nullable=False
    )


def _now() -> Any:
    return func.now()


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )


class User(Base):
    """Global identity (not tenant-owned). ADR 0004."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    # app_user has no SELECT on this column. Deferred, so plain queries never name it.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True, deferred=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )
    # app_user has no SELECT on this column either. Written by the trigger, never by the app.
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now(), deferred=True
    )

    memberships: Mapped[list["TenantMembership"]] = relationship(
        back_populates="user", lazy="raise"
    )


class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="tenant_memberships_tenant_id_id_key"),
        UniqueConstraint("tenant_id", "user_id", name="tenant_memberships_tenant_id_user_id_key"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    # One membership per user (ADR 0004, UNIQUE(user_id)).
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.users.id", **_RESTRICT),
        nullable=False,
        unique=True,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )

    user: Mapped[User] = relationship(back_populates="memberships", lazy="raise")


class TenantEmailDomain(Base):
    __tablename__ = "tenant_email_domains"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="tenant_email_domains_tenant_id_id_key"),
        UniqueConstraint("tenant_id", "domain", name="tenant_email_domains_tenant_domain_key"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="documents_tenant_id_id_key"),
        ForeignKeyConstraint(
            ["tenant_id", "current_version_id"],
            [f"{SCHEMA}.document_versions.tenant_id", f"{SCHEMA}.document_versions.id"],
            name="documents_current_version_fk",
            deferrable=True,
            initially="DEFERRED",
            **_RESTRICT,
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Set after the first version is ready. Composite FK, see __table_args__.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purge_after: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="document_versions_tenant_id_id_key"),
        UniqueConstraint(
            "document_id", "version_no", name="document_versions_document_id_version_no_key"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            [f"{SCHEMA}.documents.tenant_id", f"{SCHEMA}.documents.id"],
            name="document_versions_document_fk",
            **_RESTRICT,
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            [f"{SCHEMA}.tenant_memberships.tenant_id", f"{SCHEMA}.tenant_memberships.user_id"],
            name="document_versions_created_by_fk",
            **_RESTRICT,
        ),
        Index("document_versions_tenant_sha256_idx", "tenant_id", "sha256"),
        Index("document_versions_tenant_document_idx", "tenant_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The worker's lease on a processing version (ADR 0022). Null in every other status.
    processing_started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processing_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    retired_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purge_after: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )


class DocumentBlob(Base):
    """
    The bytes of one document version (ADR 0018). Kept apart from the version row, so queries on
    documents and versions never read the file. `content` is deferred: it is loaded only by the
    code that needs it, which is the ObjectStore (Step 2b), never by a list or a status query.
    """

    __tablename__ = "document_blobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            [f"{SCHEMA}.document_versions.tenant_id", f"{SCHEMA}.document_versions.id"],
            name="document_blobs_version_fk",
            **_RESTRICT,
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.tenants.id", **_RESTRICT),
        primary_key=True,
        nullable=False,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, nullable=False
    )
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )


class PgVector(UserDefinedType[list[float]]):
    """pgvector's `vector` type with a fixed dimension (ADR 0021). The extension lives in the `extensions`
    schema, so the type is written with that schema name."""

    cache_ok = True

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **kw: Any) -> str:
        return f"{EXTENSIONS_SCHEMA}.vector({self.dimensions})"

    def bind_processor(self, dialect: Any) -> Any:
        """A Python list becomes pgvector's text form, '[x,y,...]', which the column casts on insert."""

        def process(value: Any) -> str | None:
            if value is None:
                return None
            return "[" + ",".join(repr(float(x)) for x in value) + "]"

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: Any) -> list[float] | None:
            if value is None:
                return None
            return [float(x) for x in str(value).strip("[]").split(",")]

        return process


# Reflection (the drift test in tests/db/test_orm_metadata.py) reads the column type by its schema-qualified
# name and passes its dimension to the class. Without this entry SQLAlchemy skips the column's type, so the
# drift check would not compare the embedding dimension at all.
ischema_names["extensions.vector"] = PgVector


class Chunk(Base):
    """
    One chunk of a document version (ADR 0021): its text, the clause labels it contains (for citations),
    its embedding and the model that made it. `tsv` is generated from `body` with the English config.
    The worker writes chunks during ingestion. The app reads only ready, current, undeleted versions (RLS).
    """

    __tablename__ = "chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            [f"{SCHEMA}.document_versions.tenant_id", f"{SCHEMA}.document_versions.id"],
            name="chunks_version_fk",
            **_RESTRICT,
        ),
        # No index on the embedding: retrieval is an exact scan of the tenant's rows (ADR 0030, migration 0016).
        Index("chunks_tsv_gin", "tsv", postgresql_using="gin"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.tenants.id", **_RESTRICT),
        primary_key=True,
        nullable=False,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    section_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    heading: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    clause_labels: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(PgVector(EMBEDDING_DIMENSIONS), nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_model_version: Mapped[str] = mapped_column(Text, nullable=False)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english'::regconfig, body)", persisted=True),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="audit_log_tenant_id_id_key"),
        ForeignKeyConstraint(
            ["tenant_id", "actor_user_id"],
            [f"{SCHEMA}.tenant_memberships.tenant_id", f"{SCHEMA}.tenant_memberships.user_id"],
            name="audit_log_actor_fk",
            **_RESTRICT,
        ),
        Index("audit_log_tenant_created_idx", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    # NULL for platform-owner (admin CLI) actions and for worker actions (ADR 0007).
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ip_hmac: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # none_as_null: Python None is stored as SQL NULL, not as the JSON value null.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="refresh_tokens_tenant_id_id_key"),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            [f"{SCHEMA}.tenant_memberships.tenant_id", f"{SCHEMA}.tenant_memberships.user_id"],
            name="refresh_tokens_user_fk",
            **_RESTRICT,
        ),
        ForeignKeyConstraint(
            ["tenant_id", "replaced_by"],
            [f"{SCHEMA}.refresh_tokens.tenant_id", f"{SCHEMA}.refresh_tokens.id"],
            name="refresh_tokens_replaced_by_fk",
            **_RESTRICT,
        ),
        Index("refresh_tokens_user_id_idx", "user_id"),
        Index("refresh_tokens_family_id_idx", "family_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # SHA-256 of the opaque token. The token itself is never stored (ADR 0004).
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    issued_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="password_reset_tokens_tenant_id_id_key"),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            [f"{SCHEMA}.tenant_memberships.tenant_id", f"{SCHEMA}.tenant_memberships.user_id"],
            name="password_reset_tokens_user_fk",
            **_RESTRICT,
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            [f"{SCHEMA}.tenant_memberships.tenant_id", f"{SCHEMA}.tenant_memberships.user_id"],
            name="password_reset_tokens_created_by_fk",
            **_RESTRICT,
        ),
        Index("password_reset_tokens_user_id_idx", "user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now()
    )

"""
PostgresObjectStore: document bytes in document_blobs, inside the caller's transaction (ADR 0018).

Three rules, in code and in the database:
- The store never opens a connection. It writes on the session it is given, so the bytes commit or
  roll back with the version row and the ingestion job (the outbox guarantee).
- The storage key is never trusted from the caller. The version row holds it; the store reads the
  version's status from that row before it writes or deletes.
- A database refusal (missing privilege, failed check, failed foreign key) becomes ObjectRefused.
  The driver's message is dropped on purpose: it can carry the statement's parameters, and those
  hold ids. Nothing is logged from here.
"""

import uuid
from typing import Any

from sqlalchemy import TextClause, text
from sqlalchemy.engine import Result
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from grounded_answers.rowcount import affected_rows, expect_rowcount, expect_rowcount_in
from grounded_answers.storage.object_store import (
    ObjectNotFound,
    ObjectRefused,
    check_object_bytes,
)

# SQLSTATE codes that mean "the database refused this write": insufficient_privilege (RLS WITH
# CHECK and missing grants), foreign_key_violation, unique_violation, check_violation.
REFUSED_SQLSTATES = frozenset({"42501", "23503", "23505", "23514"})

_VERSION_STATUS = text(
    "SELECT status FROM grounded.document_versions "
    "WHERE tenant_id = :tenant_id AND id = :version_id"
)
_PUT = text(
    "INSERT INTO grounded.document_blobs (tenant_id, version_id, content, byte_size) "
    "VALUES (:tenant_id, :version_id, :content, :byte_size)"
)
_GET = text(
    "SELECT content FROM grounded.document_blobs "
    "WHERE tenant_id = :tenant_id AND version_id = :version_id"
)
_EXISTS = text(
    "SELECT 1 FROM grounded.document_blobs WHERE tenant_id = :tenant_id AND version_id = :version_id"
)
_DELETE = text(
    "DELETE FROM grounded.document_blobs WHERE tenant_id = :tenant_id AND version_id = :version_id"
)


async def _run(session: AsyncSession, statement: TextClause, params: dict[str, Any]) -> Result[Any]:
    """Run one statement. A refusal from the database becomes ObjectRefused, with no detail."""
    try:
        return await session.execute(statement, params)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in REFUSED_SQLSTATES:
            raise ObjectRefused("the database refused the operation") from None
        raise


async def _version_status(
    session: AsyncSession, tenant_id: uuid.UUID, version_id: uuid.UUID
) -> str | None:
    row = (
        await _run(
            session, _VERSION_STATUS, {"tenant_id": str(tenant_id), "version_id": str(version_id)}
        )
    ).first()
    return None if row is None else str(row[0])


class PostgresObjectStore:
    """The bytes in document_blobs. Each call uses the session it is given."""

    async def put(
        self,
        session: AsyncSession | None,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
        data: bytes,
    ) -> None:
        check_object_bytes(data)
        db = _require_session(session)
        status = await _version_status(db, tenant_id, version_id)
        if status is None:
            raise ObjectRefused("no version for this tenant")
        if status != "queued":
            raise ObjectRefused("bytes can be stored only for a queued version")
        result = await _run(
            db,
            _PUT,
            {
                "tenant_id": str(tenant_id),
                "version_id": str(version_id),
                "content": data,
                "byte_size": len(data),
            },
        )
        expect_rowcount(result, 1, "object.put")

    async def get(
        self,
        session: AsyncSession | None,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> bytes:
        db = _require_session(session)
        row = (
            await _run(db, _GET, {"tenant_id": str(tenant_id), "version_id": str(version_id)})
        ).first()
        if row is None:
            raise ObjectNotFound("no bytes are visible for this version")
        return bytes(row[0])

    async def exists(
        self,
        session: AsyncSession | None,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> bool:
        db = _require_session(session)
        row = (
            await _run(db, _EXISTS, {"tenant_id": str(tenant_id), "version_id": str(version_id)})
        ).first()
        return row is not None

    async def delete(
        self,
        session: AsyncSession | None,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> int:
        db = _require_session(session)
        status = await _version_status(db, tenant_id, version_id)
        if status is None:
            # The version row is gone. The foreign key means no bytes can exist for it.
            return 0
        if status != "purging":
            # RLS would hide the bytes of this status and the delete would match 0 rows. That must
            # never read as success, so it is refused here, before the delete (ADR 0018).
            raise ObjectRefused("bytes can be deleted only for a purging version")
        result = await _run(
            db, _DELETE, {"tenant_id": str(tenant_id), "version_id": str(version_id)}
        )
        expect_rowcount_in(result, frozenset({0, 1}), "object.delete")
        return affected_rows(result)


def _require_session(session: AsyncSession | None) -> AsyncSession:
    if session is None:
        raise TypeError("PostgresObjectStore needs the caller's transaction session")
    return session

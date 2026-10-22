"""
The storage interface for document bytes (ADR 0018).

The web app and the worker call these four methods. Two implementations exist:
- PostgresObjectStore: the bytes in document_blobs, written inside the caller's tenant or worker
  transaction (the outbox guarantee: bytes, version and job commit or roll back together);
- InMemoryObjectStore: the fake for unit tests, held to the same rules by a shared contract test.

Errors never carry document content, keys or ids in their text. Logs and tracebacks stay clean.
"""

import uuid
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

# The same limit as the database CHECK on document_blobs.byte_size and as SPEC §6.1 (10 MiB).
MAX_OBJECT_BYTES: Final = 10 * 1024 * 1024


class ObjectNotFound(Exception):
    """No bytes exist for this version, as seen by the caller's tenant and status."""


class ObjectRefused(Exception):
    """The version's status or tenant does not allow the operation, or the database refused it."""


class InvalidObject(ValueError):
    """The bytes are empty or larger than MAX_OBJECT_BYTES. Checked before anything is written."""


def check_object_bytes(data: bytes) -> None:
    """Refuse empty and oversized content. The same rule the database applies (ADR 0018)."""
    if not data:
        raise InvalidObject("the file is empty")
    if len(data) > MAX_OBJECT_BYTES:
        raise InvalidObject("the file is larger than 10 MiB")


class ObjectStore(Protocol):
    """
    The four operations on one version's bytes.

    `session` is the caller's transaction (PostgresObjectStore) or None (InMemoryObjectStore).
    The store never opens its own connection, so a rollback of the caller's transaction also
    removes any bytes it wrote.
    """

    async def put(
        self,
        session: AsyncSession | None,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
        data: bytes,
    ) -> None:
        """Store the bytes of a queued version. Refused for any other status or tenant."""
        ...

    async def get(
        self,
        session: AsyncSession | None,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> bytes:
        """Return the bytes. Raises ObjectNotFound when none are visible to the caller."""
        ...

    async def exists(
        self,
        session: AsyncSession | None,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> bool:
        """True when the bytes are visible to the caller."""
        ...

    async def delete(
        self,
        session: AsyncSession | None,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> int:
        """
        Remove the bytes of a purging version. Returns the rows removed: 1, or 0 when they were
        already gone (idempotent, ADR 0007). Refused when the version is not purging.
        """
        ...

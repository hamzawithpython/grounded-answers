"""
InMemoryObjectStore: the unit-test fake for ObjectStore (ADR 0018).

It enforces the same rules as PostgresObjectStore, so tests of the upload and purge code cannot pass
against the fake and then fail in production. A shared contract test (tests/db/test_object_store_contract.py)
runs the same cases against both implementations. If the two ever disagree, that test fails.

What the fake mirrors from the database:
- put only for a version in 'queued', in its own tenant;
- get and exists only for a version the worker can see: 'queued', 'processing' or 'purging';
- delete for a 'purging' version only, 0 when the bytes are already gone, and a refusal otherwise;
- a version cannot be forgotten while its bytes exist (the RESTRICT foreign key).

What it cannot mirror: transactions. A rollback cannot undo a put here. The rollback guarantee is
tested only against PostgresObjectStore (tests/db/test_postgres_store_outbox.py).
"""

import uuid
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from grounded_answers.storage.object_store import (
    ObjectNotFound,
    ObjectRefused,
    check_object_bytes,
)

# The statuses in which the worker can read a version's bytes (the policy in migration 0007).
_WORKER_READABLE: Final = frozenset({"queued", "processing", "purging"})


class InMemoryObjectStore:
    """Bytes and versions held in dictionaries. Test use only: nothing is persisted."""

    def __init__(self) -> None:
        self._versions: dict[tuple[uuid.UUID, uuid.UUID], str] = {}
        self._bytes: dict[tuple[uuid.UUID, uuid.UUID], bytes] = {}

    def register_version(self, *, tenant_id: uuid.UUID, version_id: uuid.UUID, status: str) -> None:
        """Stand-in for a document_versions row. The contract harness calls this, not the app."""
        self._versions[(tenant_id, version_id)] = status

    def has_bytes(self, *, tenant_id: uuid.UUID, version_id: uuid.UUID) -> bool:
        """Test helper: whether bytes are stored, whatever the visibility rules say."""
        return (tenant_id, version_id) in self._bytes

    def forget_version(self, *, tenant_id: uuid.UUID, version_id: uuid.UUID) -> None:
        """Stand-in for deleting a version row. Refused while its bytes exist (RESTRICT)."""
        key = (tenant_id, version_id)
        if key in self._bytes:
            raise ObjectRefused("the version still has bytes")
        self._versions.pop(key, None)

    async def put(
        self,
        session: AsyncSession | None,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
        data: bytes,
    ) -> None:
        check_object_bytes(data)
        key = (tenant_id, version_id)
        status = self._versions.get(key)
        if status is None:
            raise ObjectRefused("no version for this tenant")
        if status != "queued":
            raise ObjectRefused("bytes can be stored only for a queued version")
        self._bytes[key] = bytes(data)

    async def get(
        self,
        session: AsyncSession | None,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> bytes:
        key = (tenant_id, version_id)
        if not self._visible(key) or key not in self._bytes:
            raise ObjectNotFound("no bytes are visible for this version")
        return self._bytes[key]

    async def exists(
        self,
        session: AsyncSession | None,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> bool:
        key = (tenant_id, version_id)
        return self._visible(key) and key in self._bytes

    async def delete(
        self,
        session: AsyncSession | None,
        *,
        tenant_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> int:
        key = (tenant_id, version_id)
        status = self._versions.get(key)
        if status is None:
            return 0
        if status != "purging":
            raise ObjectRefused("bytes can be deleted only for a purging version")
        if key in self._bytes:
            del self._bytes[key]
            return 1
        return 0

    def _visible(self, key: tuple[uuid.UUID, uuid.UUID]) -> bool:
        return self._versions.get(key) in _WORKER_READABLE

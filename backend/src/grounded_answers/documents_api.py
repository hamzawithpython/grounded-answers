"""
POST /documents: upload one document (SPEC §6.1, ADR 0019).

Order of checks, cheapest first, so a refused request does least work:
1. the feature switch (UPLOAD_ENABLED, default off): 404 when off;
2. the caller is a tenant_admin, from the database (AuthenticatedRequest): 403 otherwise;
3. the upload rate limit (per tenant and per user): 429, or 503 when Valkey is unavailable;
4. the form, with limits on fields and files: 400 when malformed;
5. the file: 400 when missing or empty, 413 when over 10 MiB (the body cap in UploadBodyLimit has
   already stopped a larger body while it streamed);
6. the type from the bytes and the extension: 415 when refused;
7. one tenant transaction: the duplicate check, then the document, the queued version, the bytes,
   the audit row and the ingestion job. Any failure rolls all of them back.

Responses carry a stable shape: {"detail": "..."} (plus document_id on 409). No internal detail.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException

from grounded_answers.auth_api import AuthenticatedRequest
from grounded_answers.config import get_settings
from grounded_answers.jobs.enqueue import enqueue_job
from grounded_answers.jobs.tasks import INGEST_DOCUMENT
from grounded_answers.rate_limit import LimiterUnavailable
from grounded_answers.rowcount import affected_rows, expect_rowcount
from grounded_answers.storage.object_store import ObjectStore
from grounded_answers.upload.detect import CONTENT_TYPE, Kind, UnsupportedFileType, detect_kind
from grounded_answers.upload.limits import (
    MAX_FILE_BYTES,
    MAX_FORM_FIELDS,
    MAX_FORM_FILES,
    MAX_FORM_PART_BYTES,
)
from grounded_answers.upload.rate import UploadLimiter
from grounded_answers.upload.sanitize import sanitize_filename

router = APIRouter(prefix="/documents", tags=["documents"])

_TITLE_CHARS: Final = 200
_ORIGINAL_FILENAME_CHARS: Final = 255


_FIND_DUPLICATE: Final = text(
    "SELECT d.id FROM grounded.documents AS d "
    "JOIN grounded.document_versions AS v ON v.document_id = d.id AND v.tenant_id = d.tenant_id "
    "WHERE d.tenant_id = :tenant_id AND v.sha256 = :sha256 AND d.deleted_at IS NULL "
    "AND v.status IN ('queued', 'processing', 'ready') LIMIT 1"
)
_LOCK_TENANT_HASH: Final = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
_INSERT_DOCUMENT: Final = text(
    "INSERT INTO grounded.documents (id, tenant_id, title) VALUES (:id, :tenant_id, :title)"
)
_INSERT_VERSION: Final = text(
    "INSERT INTO grounded.document_versions (id, tenant_id, document_id, version_no, "
    "original_filename, content_type, size_bytes, sha256, storage_key, status, created_by) "
    "VALUES (:id, :tenant_id, :document_id, :version_no, :original_filename, :content_type, :size_bytes, "
    ":sha256, :storage_key, 'queued', :created_by)"
)
_INSERT_AUDIT: Final = text(
    "INSERT INTO grounded.audit_log (id, tenant_id, actor_user_id, action, target_type, target_id, "
    "detail) VALUES (:id, :tenant_id, :actor_user_id, 'document.upload', 'document', :target_id, "
    "CAST(:detail AS jsonb))"
)


def _error(status: int, detail: str, **extra: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, **extra})


@dataclass(frozen=True)
class _Upload:
    """A checked file: its bytes, the sanitised name, the detected kind, and the sha256 of the bytes."""

    data: bytes
    name: str
    kind: Kind
    digest: bytes


async def _admit_upload(
    request: Request, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> JSONResponse | None:
    """The upload rate limit (ADR 0019). None when the request may go on."""
    limiter: UploadLimiter = request.app.state.upload_limiter
    try:
        retry_after = await limiter.admit(tenant_id, user_id)
    except LimiterUnavailable:
        return _error(503, "Service unavailable")
    if retry_after is not None:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many uploads. Try again later."},
            headers={"Retry-After": str(retry_after)},
        )
    return None


async def _read_upload(request: Request) -> _Upload | JSONResponse:
    """The form's file, with every limit of POST /documents (ADR 0019). Shared with replacement (ADR 0024)."""
    try:
        form = await request.form(
            max_files=MAX_FORM_FILES,
            max_fields=MAX_FORM_FIELDS,
            max_part_size=MAX_FORM_PART_BYTES,
        )
    except (MultiPartException, HTTPException):
        # Inside an app, Starlette turns a parser error into HTTPException, with the parser's text
        # as its detail. Both are answered with one generic message, and no parser text leaves.
        return _error(400, "The form is not valid")
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return _error(400, "A file is required")

    data = await upload.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        return _error(413, "The file is larger than 10 MiB")
    if not data:
        return _error(400, "The file is empty")

    name = sanitize_filename(upload.filename or "")
    try:
        kind = detect_kind(data, name)
    except UnsupportedFileType:
        return _error(415, "This file type is not supported")
    return _Upload(data=data, name=name, kind=kind, digest=hashlib.sha256(data).digest())


@router.post("", response_model=None, status_code=201)
async def upload_document(request: Request, ctx: AuthenticatedRequest) -> Response:
    if not get_settings().upload_enabled:
        return _error(404, "Not Found")
    principal = ctx.principal
    if principal.role != "tenant_admin":
        return _error(403, "Forbidden")

    refused = await _admit_upload(request, principal.tenant_id, principal.user_id)
    if refused is not None:
        return refused
    file = await _read_upload(request)
    if isinstance(file, JSONResponse):
        return file
    data, name, kind, digest = file.data, file.name, file.kind, file.digest
    session = ctx.session
    tenant_id = principal.tenant_id

    await session.execute(_LOCK_TENANT_HASH, {"key": f"{tenant_id}:{digest.hex()}"})
    duplicate = (
        await session.execute(_FIND_DUPLICATE, {"tenant_id": str(tenant_id), "sha256": digest})
    ).first()
    if duplicate is not None:
        return _error(409, "This file was already uploaded", document_id=str(duplicate[0]))

    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    storage_key = f"tenant/{tenant_id}/{uuid.uuid4()}"
    title = name[:_TITLE_CHARS]
    await session.execute(
        _INSERT_DOCUMENT, {"id": str(document_id), "tenant_id": str(tenant_id), "title": title}
    )
    await session.execute(
        _INSERT_VERSION,
        {
            "id": str(version_id),
            "tenant_id": str(tenant_id),
            "document_id": str(document_id),
            "original_filename": name[:_ORIGINAL_FILENAME_CHARS],
            "content_type": CONTENT_TYPE[kind],
            "size_bytes": len(data),
            "sha256": digest,
            "storage_key": storage_key,
            "created_by": str(principal.user_id),
            "version_no": 1,
        },
    )
    store: ObjectStore = request.app.state.object_store
    await store.put(session, tenant_id=tenant_id, version_id=version_id, data=data)
    await session.execute(
        _INSERT_AUDIT,
        {
            "id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "actor_user_id": str(principal.user_id),
            "target_id": str(document_id),
            "detail": json.dumps({"size_bytes": len(data), "content_type": CONTENT_TYPE[kind]}),
        },
    )
    job_app = request.app.state.job_app
    await enqueue_job(
        session,
        job_app,
        INGEST_DOCUMENT,
        {"tenant_id": tenant_id, "document_id": document_id, "version_id": version_id},
    )
    body: dict[str, Any] = {
        "document_id": str(document_id),
        "version_id": str(version_id),
        "status": "queued",
    }
    return JSONResponse(status_code=201, content=body)


# ---- DELETE /documents/{id} (ADR 0025) ----

# Marks a live document deleted. The condition deleted_at IS NULL makes a second delete change nothing.
_MARK_DOCUMENT_DELETED: Final = text(
    "UPDATE grounded.documents SET deleted_at = now(), "
    "purge_after = now() + make_interval(days => :days) "
    "WHERE id = :document_id AND tenant_id = :tenant_id AND deleted_at IS NULL"
)
# Retires every version that is not already retired or purging. Those keep the retirement time they have.
_RETIRE_VERSIONS: Final = text(
    "UPDATE grounded.document_versions SET status = 'retired', retired_at = now(), "
    "purge_after = now() + make_interval(days => :days) "
    "WHERE document_id = :document_id AND tenant_id = :tenant_id "
    "AND status IN ('queued', 'processing', 'ready', 'failed')"
)
_INSERT_DELETE_AUDIT: Final = text(
    "INSERT INTO grounded.audit_log (id, tenant_id, actor_user_id, action, target_type, target_id, "
    "detail) VALUES (:id, :tenant_id, :actor_user_id, 'document.delete', 'document', :target_id, "
    "CAST(:detail AS jsonb))"
)


async def retire_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    retention_days: int,
) -> int | None:
    """
    Delete one live document inside the caller's tenant transaction (ADR 0025). Returns the number of versions
    retired, or None when there was no live document to delete (unknown, already deleted, or another tenant's).

    Nothing is written in the None case. The audit row holds counts only: no title, no filename.
    """
    marked = await session.execute(
        _MARK_DOCUMENT_DELETED,
        {
            "days": retention_days,
            "document_id": str(document_id),
            "tenant_id": str(tenant_id),
        },
    )
    if affected_rows(marked) == 0:
        return None
    expect_rowcount(marked, 1, "document.delete")
    retired = await session.execute(
        _RETIRE_VERSIONS,
        {
            "days": retention_days,
            "document_id": str(document_id),
            "tenant_id": str(tenant_id),
        },
    )
    versions_retired = affected_rows(retired)
    await session.execute(
        _INSERT_DELETE_AUDIT,
        {
            "id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "actor_user_id": str(actor_user_id),
            "target_id": str(document_id),
            "detail": json.dumps({"versions_retired": versions_retired}),
        },
    )
    return versions_retired


@router.delete("/{document_id}", response_model=None, status_code=204)
async def delete_document(document_id: uuid.UUID, ctx: AuthenticatedRequest) -> Response:
    """
    Delete a document: 204 when it was live, 404 otherwise.

    404 answers an unknown document, a document that is already deleted, and another tenant's document with the
    same body. RLS hides another tenant's rows, so the first two cases are the only ones the caller can distinguish.
    A repeated delete is a 404 rather than a 204, because after the first delete the document is absent to every
    read: retrieval excludes it (ARCHITECTURE 5.6), and GET /documents will too (PR 5, ADR 0029). A 204 would say
    that a deleted document id still names something. A 404 says only what a read would say. The state is the same either way, so the operation
    stays idempotent: deleting twice leaves one deleted document.
    """
    principal = ctx.principal
    if principal.role != "tenant_admin":
        return _error(403, "Forbidden")
    versions = await retire_document(
        ctx.session,
        tenant_id=principal.tenant_id,
        document_id=document_id,
        actor_user_id=principal.user_id,
        retention_days=get_settings().document_retention_days,
    )
    if versions is None:
        return _error(404, "Not Found")
    return Response(status_code=204)


# ---- POST /documents/{id}/versions (ADR 0024) ----

# The document row is the one serialization point for its versions (ADR 0024). A replacement takes it, and so do the
# swap at ingestion finish and a delete. Two replacements therefore cannot read the same maximum version number.
_LOCK_LIVE_DOCUMENT: Final = text(
    "SELECT current_version_id FROM grounded.documents WHERE id = :document_id AND tenant_id = :tenant_id "
    "AND deleted_at IS NULL FOR UPDATE"
)
_CURRENT_VERSION: Final = text(
    "SELECT id, sha256 FROM grounded.document_versions WHERE id = :version_id AND tenant_id = :tenant_id"
)
_NEXT_VERSION_NUMBER: Final = text(
    "SELECT COALESCE(MAX(version_no), 0) + 1 FROM grounded.document_versions "
    "WHERE document_id = :document_id AND tenant_id = :tenant_id"
)
_INSERT_REPLACEMENT_AUDIT: Final = text(
    "INSERT INTO grounded.audit_log (id, tenant_id, actor_user_id, action, target_type, target_id, "
    "detail) VALUES (:id, :tenant_id, :actor_user_id, 'document_version.upload', 'document_version', "
    ":target_id, CAST(:detail AS jsonb))"
)


@router.post("/{document_id}/versions", response_model=None, status_code=201)
async def replace_document(
    document_id: uuid.UUID, request: Request, ctx: AuthenticatedRequest
) -> Response:
    """
    Upload a new version of a live document (ADR 0024). It is queued, with version_no = the highest + 1, and its
    ingestion job is enqueued in the same transaction. The swap at finish decides whether it becomes current.

    404 for a missing, deleted or another tenant's document (the same answer, as for DELETE). 409 with the current
    version's ids when the bytes equal the current version's.
    """
    if not get_settings().upload_enabled:
        return _error(404, "Not Found")
    principal = ctx.principal
    if principal.role != "tenant_admin":
        return _error(403, "Forbidden")
    refused = await _admit_upload(request, principal.tenant_id, principal.user_id)
    if refused is not None:
        return refused
    file = await _read_upload(request)
    if isinstance(file, JSONResponse):
        return file

    session = ctx.session
    tenant_id = principal.tenant_id
    locked = (
        await session.execute(
            _LOCK_LIVE_DOCUMENT, {"document_id": str(document_id), "tenant_id": str(tenant_id)}
        )
    ).first()
    if locked is None:
        return _error(404, "Not Found")
    if locked[0] is not None:
        current = (
            await session.execute(
                _CURRENT_VERSION, {"version_id": str(locked[0]), "tenant_id": str(tenant_id)}
            )
        ).first()
        if current is not None and bytes(current[1]) == file.digest:
            return _error(
                409,
                "This file is already the current version",
                document_id=str(document_id),
                version_id=str(current[0]),
            )
    version_no = (
        await session.execute(
            _NEXT_VERSION_NUMBER, {"document_id": str(document_id), "tenant_id": str(tenant_id)}
        )
    ).scalar_one()

    version_id = uuid.uuid4()
    await session.execute(
        _INSERT_VERSION,
        {
            "id": str(version_id),
            "tenant_id": str(tenant_id),
            "document_id": str(document_id),
            "version_no": version_no,
            "original_filename": file.name[:_ORIGINAL_FILENAME_CHARS],
            "content_type": CONTENT_TYPE[file.kind],
            "size_bytes": len(file.data),
            "sha256": file.digest,
            "storage_key": f"tenant/{tenant_id}/{uuid.uuid4()}",
            "created_by": str(principal.user_id),
        },
    )
    store: ObjectStore = request.app.state.object_store
    await store.put(session, tenant_id=tenant_id, version_id=version_id, data=file.data)
    await session.execute(
        _INSERT_REPLACEMENT_AUDIT,
        {
            "id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "actor_user_id": str(principal.user_id),
            "target_id": str(version_id),
            "detail": json.dumps({"size_bytes": len(file.data), "version_no": version_no}),
        },
    )
    await enqueue_job(
        session,
        request.app.state.job_app,
        INGEST_DOCUMENT,
        {"tenant_id": tenant_id, "document_id": document_id, "version_id": version_id},
    )
    return JSONResponse(
        status_code=201,
        content={
            "document_id": str(document_id),
            "version_id": str(version_id),
            "version_no": version_no,
            "status": "queued",
        },
    )

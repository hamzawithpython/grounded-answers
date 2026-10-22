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
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException

from grounded_answers.auth_api import AuthenticatedRequest
from grounded_answers.config import get_settings
from grounded_answers.jobs.enqueue import enqueue_job
from grounded_answers.jobs.tasks import INGEST_DOCUMENT
from grounded_answers.rate_limit import LimiterUnavailable
from grounded_answers.storage.object_store import ObjectStore
from grounded_answers.upload.detect import CONTENT_TYPE, UnsupportedFileType, detect_kind
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
    "VALUES (:id, :tenant_id, :document_id, 1, :original_filename, :content_type, :size_bytes, "
    ":sha256, :storage_key, 'queued', :created_by)"
)
_INSERT_AUDIT: Final = text(
    "INSERT INTO grounded.audit_log (id, tenant_id, actor_user_id, action, target_type, target_id, "
    "detail) VALUES (:id, :tenant_id, :actor_user_id, 'document.upload', 'document', :target_id, "
    "CAST(:detail AS jsonb))"
)


def _error(status: int, detail: str, **extra: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, **extra})


@router.post("", response_model=None, status_code=201)
async def upload_document(request: Request, ctx: AuthenticatedRequest) -> Response:
    if not get_settings().upload_enabled:
        return _error(404, "Not Found")
    principal = ctx.principal
    if principal.role != "tenant_admin":
        return _error(403, "Forbidden")

    limiter: UploadLimiter = request.app.state.upload_limiter
    try:
        retry_after = await limiter.admit(principal.tenant_id, principal.user_id)
    except LimiterUnavailable:
        return _error(503, "Service unavailable")
    if retry_after is not None:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many uploads. Try again later."},
            headers={"Retry-After": str(retry_after)},
        )

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

    digest = hashlib.sha256(data).digest()
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

"""
GET /documents: the tenant admin's list of live documents (ADR 0029).

Each item shows the document, its current version's status (null until a version is current), and the latest
version (the highest version_no): its id, number, status and a generic failure reason. So Step 8 can see a queued,
processing or failed version before the document has a current version, and a replacement that failed.

Order and pages: newest first, by (created_at, id). The cursor is the (created_at, id) of the last item, encoded
opaquely. A page is "strictly after this key", so a document added between two requests lands above the cursor and
is never repeated or skipped. Limit: 1 to 50, default 20.

The cursor carries a position only. It grants nothing: RLS still decides which rows the caller sees. A malformed
cursor or limit is 400 with a generic message.

failure_reason is returned only when it is one of the generic reasons (FAILURE_REASONS, ADR 0020). Any other stored
text is returned as null, and is never echoed.
"""

import base64
import binascii
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Final

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from grounded_answers.auth_api import AuthenticatedRequest
from grounded_answers.ingest.failures import FAILURE_REASONS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 50

_SELECT_LIVE: Final = (
    "SELECT d.id, d.title, d.created_at, d.updated_at, d.current_version_id, cv.status AS current_status, "
    "lv.id AS latest_id, lv.version_no AS latest_version_no, lv.status AS latest_status, "
    "lv.failure_reason AS latest_failure_reason "
    "FROM grounded.documents AS d "
    "LEFT JOIN grounded.document_versions AS cv "
    "ON cv.tenant_id = d.tenant_id AND cv.id = d.current_version_id "
    "LEFT JOIN LATERAL (SELECT v.id, v.version_no, v.status, v.failure_reason "
    "FROM grounded.document_versions AS v "
    "WHERE v.tenant_id = d.tenant_id AND v.document_id = d.id "
    "ORDER BY v.version_no DESC LIMIT 1) AS lv ON true "
    "WHERE d.deleted_at IS NULL"
)
_FIRST_PAGE: Final = text(_SELECT_LIVE + " ORDER BY d.created_at DESC, d.id DESC LIMIT :limit")
_NEXT_PAGE: Final = text(
    _SELECT_LIVE
    + " AND (d.created_at, d.id) < (CAST(:after_created AS timestamptz), CAST(:after_id AS uuid))"
    + " ORDER BY d.created_at DESC, d.id DESC LIMIT :limit"
)


class InvalidRequest(Exception):
    """The limit or the cursor is malformed. The message is generic and says nothing about the stored data."""


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


def parse_limit(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    try:
        limit = int(raw)
    except ValueError as exc:
        raise InvalidRequest("limit must be a whole number from 1 to 50") from exc
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidRequest("limit must be a whole number from 1 to 50")
    return limit


def encode_cursor(created_at: datetime, document_id: uuid.UUID) -> str:
    payload = json.dumps({"c": created_at.isoformat(), "i": str(document_id)}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_cursor(raw: str) -> tuple[datetime, uuid.UUID]:
    """The (created_at, id) a cursor points after. Any malformed value raises InvalidRequest."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
        created_at = datetime.fromisoformat(payload["c"])
        if created_at.tzinfo is None:
            raise ValueError("the cursor time has no time zone")
        return created_at, uuid.UUID(payload["i"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError) as exc:
        raise InvalidRequest("the cursor is not valid") from exc


def _reason_for_caller(stored: str | None) -> str | None:
    """Only a generic reason leaves the server. Anything else stored is not echoed."""
    if stored is None:
        return None
    if stored in FAILURE_REASONS:
        return stored
    logger.warning("documents.list.unexpected_failure_reason")
    return None


def _item(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "title": row.title,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "current_version_status": row.current_status,
        "latest_version_id": str(row.latest_id) if row.latest_id is not None else None,
        "latest_version_no": row.latest_version_no,
        "latest_version_status": row.latest_status,
        "latest_failure_reason": _reason_for_caller(row.latest_failure_reason),
    }


@router.get("", response_model=None)
async def list_documents(
    ctx: AuthenticatedRequest,
    limit: str | None = None,
    cursor: str | None = None,
) -> JSONResponse:
    principal = ctx.principal
    if principal.role != "tenant_admin":
        return _error(403, "Forbidden")
    try:
        page_size = parse_limit(limit)
        if cursor is None:
            statement = _FIRST_PAGE
            params: dict[str, Any] = {"limit": page_size + 1}
        else:
            after_created, after_id = decode_cursor(cursor)
            statement = _NEXT_PAGE
            params = {
                "limit": page_size + 1,
                "after_created": after_created,
                "after_id": str(after_id),
            }
    except InvalidRequest as exc:
        return _error(400, str(exc))

    rows = (await ctx.session.execute(statement, params)).all()
    has_more = len(rows) > page_size
    page = rows[:page_size]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
    return JSONResponse(
        status_code=200,
        content={"items": [_item(row) for row in page], "next_cursor": next_cursor},
    )

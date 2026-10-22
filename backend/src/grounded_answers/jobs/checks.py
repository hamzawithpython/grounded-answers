"""
Helpers a background task calls inside its worker transaction (ADR 0015 decision 9, ADR 0017).

The job's tenant id is untrusted: a job carries it, and a bug or a forged id could name another
tenant. So a task first sets the tenant with begin_worker_transaction, then looks the document up.
Row-level security hides every row outside that tenant. A document that is not found is therefore
either a wrong document id or a wrong tenant id. Either way the job is refused.

Refusal is a permanent failure: DocumentNotFoundForTenant is not in any retry list, so Procrastinate
ends the job as failed at once (ADR 0015 decision 9).
"""

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# The log line for a refused job. It names the reason and holds no ids (ADR 0015 decision 9).
REFUSED_LOG = "ingest.refused reason=document_not_in_tenant"

_FIND_DOCUMENT = text("SELECT id FROM grounded.documents WHERE id = :document_id")


class DocumentNotFoundForTenant(Exception):
    """The document is not visible under the job's tenant. The job is refused, never retried."""


async def load_document_for_worker(session: AsyncSession, document_id: uuid.UUID) -> uuid.UUID:
    """
    Return the document's id if the session's tenant can see it. Call inside begin_worker_transaction.

    Raises DocumentNotFoundForTenant otherwise, after a log line that holds no ids.
    """
    row = (await session.execute(_FIND_DOCUMENT, {"document_id": str(document_id)})).first()
    if row is None:
        logger.warning(REFUSED_LOG)
        raise DocumentNotFoundForTenant("document not found for this tenant")
    return uuid.UUID(str(row[0]))

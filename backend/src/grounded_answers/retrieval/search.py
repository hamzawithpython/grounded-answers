"""
Retrieve the chunks that may answer a question (ADR 0030): hybrid search, vector and keyword fused by reciprocal rank.

The caller passes a session from `begin_tenant_transaction` and the question's embedding. The session carries the
tenant, so row-level security keeps only that tenant's chunks of current, ready versions of documents that are not
deleted (ARCHITECTURE 5.6). This module adds no tenant or version filter of its own, and it does not open a
transaction: the caller owns it, as with the object store (ADR 0018).

What comes back is exactly top_k chunks, best first, or fewer when the tenant has fewer visible chunks. Each carries
the text, the document title, the version number and the clause labels, which the answer step needs for citations.

Repeatable: equal scores are broken by the md5 of the chunk text (rankers.py), so the same documents and the same
question give the same order.

A chunk can stop being current between the ranking query and the metadata query, if a replacement or a delete
commits in between (the transaction runs at READ COMMITTED). It is then left out, so the result can be shorter than
top_k in that rare case. It is never a retired chunk.

Only counts are logged. The question and the chunk text are not.
"""

import logging
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from grounded_answers.models import EMBEDDING_DIMENSIONS
from grounded_answers.retrieval.rankers import RankedRow, fuse, keyword_rank, vector_rank

logger = logging.getLogger(__name__)

# Rows taken from each ranker before fusion. Measured in docs/EVAL.md at depth 50.
DEFAULT_DEPTH: Final = 50
MAX_QUESTION_CHARS: Final = 2000

_METADATA = text(
    """
    SELECT c.version_id, c.position, c.section_number, c.heading, c.clause_labels, c.body,
           d.id AS document_id, d.title, v.version_no
    FROM unnest(CAST(:version_ids AS uuid[]), CAST(:positions AS integer[])) AS k (version_id, position)
    JOIN grounded.chunks AS c ON c.version_id = k.version_id AND c.position = k.position
    JOIN grounded.document_versions AS v ON v.tenant_id = c.tenant_id AND v.id = c.version_id
    JOIN grounded.documents AS d ON d.tenant_id = v.tenant_id AND d.id = v.document_id
    """
)


class RetrievalInputError(ValueError):
    """The question, the vector or the count is not usable. Nothing was sent to the database."""


@dataclass(frozen=True)
class RetrievedChunk:
    version_id: uuid.UUID
    position: int
    document_id: uuid.UUID
    title: str
    version_no: int
    section_number: str | None
    heading: str
    clause_labels: tuple[str, ...]
    body: str
    # The fused (reciprocal rank) score. Only the order across one result is meaningful.
    score: float
    # 1-based rank in each ranker's list, None when the ranker did not return the chunk.
    vector_rank: int | None
    keyword_rank: int | None


def _ranks(rows: Sequence[RankedRow]) -> dict[tuple[uuid.UUID, int], int]:
    return {(row.version_id, row.position): rank for rank, row in enumerate(rows, start=1)}


def _validate(question: str, query_vector: Sequence[float], top_k: int, depth: int) -> None:
    if not question.strip():
        raise RetrievalInputError("the question is empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise RetrievalInputError(f"the question is longer than {MAX_QUESTION_CHARS} characters")
    if len(query_vector) != EMBEDDING_DIMENSIONS:
        raise RetrievalInputError(f"the query vector must have {EMBEDDING_DIMENSIONS} dimensions")
    if not all(math.isfinite(value) for value in query_vector):
        raise RetrievalInputError("the query vector holds a value that is not a finite number")
    if not 1 <= top_k <= depth:
        raise RetrievalInputError(f"top_k must be between 1 and the depth ({depth})")
    if depth < 1:
        raise RetrievalInputError("the depth must be at least 1")


async def retrieve(
    session: AsyncSession,
    question: str,
    query_vector: Sequence[float],
    *,
    top_k: int,
    depth: int = DEFAULT_DEPTH,
) -> list[RetrievedChunk]:
    _validate(question, query_vector, top_k, depth)
    vector = await vector_rank(session, query_vector, depth)
    keyword = await keyword_rank(session, question, depth)
    fused = fuse([vector, keyword], depth)[:top_k]
    if not fused:
        logger.info(
            "retrieval.done returned=0 vector_rows=%d keyword_rows=%d", len(vector), len(keyword)
        )
        return []

    result = await session.execute(
        _METADATA,
        {
            "version_ids": [row.version_id for row in fused],
            "positions": [row.position for row in fused],
        },
    )
    by_key = {(row.version_id, row.position): row for row in result.all()}
    vector_ranks, keyword_ranks = _ranks(vector), _ranks(keyword)
    chunks: list[RetrievedChunk] = []
    for ranked in fused:
        key = (ranked.version_id, ranked.position)
        meta = by_key.get(key)
        if meta is None:
            continue
        chunks.append(
            RetrievedChunk(
                version_id=ranked.version_id,
                position=ranked.position,
                document_id=meta.document_id,
                title=meta.title,
                version_no=int(meta.version_no),
                section_number=meta.section_number,
                heading=meta.heading,
                clause_labels=tuple(str(label) for label in meta.clause_labels),
                body=meta.body,
                score=ranked.score,
                vector_rank=vector_ranks.get(key),
                keyword_rank=keyword_ranks.get(key),
            )
        )
    logger.info(
        "retrieval.done returned=%d vector_rows=%d keyword_rows=%d",
        len(chunks),
        len(vector),
        len(keyword),
    )
    return chunks

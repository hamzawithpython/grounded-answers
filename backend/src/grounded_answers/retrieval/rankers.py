"""
The rankers behind retrieval (ADR 0030). Each runs inside a tenant transaction as app_user, so row-level security and
the current-version rule apply (ARCHITECTURE 5.6). No ranker filters by tenant or version itself: the policy on
`chunks` does.

- vector: cosine distance between the query embedding and the chunk embedding (pgvector). The score is the cosine
  similarity, 1 - distance. There is no vector index (migration 0016), so the planner narrows by the tenant-first
  primary key and sorts that tenant's rows by exact distance.
- keyword: PostgreSQL full-text search over the stored tsvector (English configuration). The question's words
  are stemmed by the same configuration, then joined with OR, because a question has words that no single chunk
  contains. Ranked with ts_rank_cd. PostgreSQL has no inverse document frequency in its rank functions, so this is
  not BM25 (docs/EVAL.md). The English configuration cannot match Roman Urdu or Urdu script (ADR 0021).
- hybrid: reciprocal rank fusion of the two lists: score = sum over lists of 1 / (60 + rank). A chunk missing from a
  list adds nothing from it, so a question with no keyword match keeps the vector order.

Ties are common (ts_rank_cd gives coarse scores, and fusion makes equal sums). They are broken by the md5 of the
chunk body, then (version_id, position). The md5 comes from the text, so it is the same on every load of the same
documents; version_id is a random UUID and alone would make two loads of the same files rank differently.

The question text is bound as a parameter, never joined into the SQL. Nothing here is logged.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

RRF_K: Final = 60

# The vector type and its operators live in the 'extensions' schema (migration 0008), which is not on
# app_user's search_path, so they are qualified.
_VECTOR_SQL = text(
    """
    SELECT c.version_id, c.position, c.clause_labels, md5(c.body) AS tie_key,
           1 - (c.embedding OPERATOR(extensions.<=>) CAST(:vector AS extensions.vector)) AS score
    FROM grounded.chunks AS c
    ORDER BY c.embedding OPERATOR(extensions.<=>) CAST(:vector AS extensions.vector),
             tie_key, c.version_id, c.position
    LIMIT :depth
    """
)

# to_tsvector gives the stemmed words without stop words. Each lexeme is quoted, and the OR query is cast
# straight to tsquery, so the words are not stemmed a second time. No words means a NULL query and no rows.
_KEYWORD_SQL = text(
    """
    WITH q AS (
      SELECT string_agg('''' || replace(lexeme, '''', '''''') || '''', ' | ')::tsquery AS tsq
      FROM unnest(to_tsvector('english', :question))
    )
    SELECT c.version_id, c.position, c.clause_labels, md5(c.body) AS tie_key,
           ts_rank_cd(c.tsv, q.tsq) AS score
    FROM grounded.chunks AS c, q
    WHERE c.tsv @@ q.tsq
    ORDER BY score DESC, tie_key, c.version_id, c.position
    LIMIT :depth
    """
)


@dataclass(frozen=True)
class RankedRow:
    version_id: uuid.UUID
    position: int
    clause_labels: tuple[str, ...]
    score: float
    # md5 of the chunk body: a tie-break that does not depend on random ids.
    tie_key: str = ""


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text form: [0.1,0.2,...]."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def _rows(result_rows: Sequence[Row[Any]]) -> list[RankedRow]:
    return [
        RankedRow(
            row.version_id,
            int(row.position),
            tuple(str(label) for label in row.clause_labels),
            float(row.score),
            str(row.tie_key),
        )
        for row in result_rows
    ]


async def vector_rank(
    session: AsyncSession, query_vector: Sequence[float], depth: int
) -> list[RankedRow]:
    result = await session.execute(
        _VECTOR_SQL, {"vector": vector_literal(query_vector), "depth": depth}
    )
    return _rows(result.all())


async def keyword_rank(session: AsyncSession, question: str, depth: int) -> list[RankedRow]:
    result = await session.execute(_KEYWORD_SQL, {"question": question, "depth": depth})
    return _rows(result.all())


def fuse(
    lists: Sequence[Sequence[RankedRow]], depth: int, constant: int = RRF_K
) -> list[RankedRow]:
    """Reciprocal rank fusion. A chunk missing from a list adds nothing from that list."""
    scores: dict[tuple[uuid.UUID, int], float] = defaultdict(float)
    rows: dict[tuple[uuid.UUID, int], RankedRow] = {}
    for ranked in lists:
        for rank, row in enumerate(ranked, start=1):
            key = (row.version_id, row.position)
            scores[key] += 1.0 / (constant + rank)
            rows[key] = row
    ordered = sorted(scores, key=lambda key: (-scores[key], rows[key].tie_key, str(key[0]), key[1]))
    return [
        RankedRow(key[0], key[1], rows[key].clause_labels, scores[key], rows[key].tie_key)
        for key in ordered[:depth]
    ]


async def hybrid_rank(
    session: AsyncSession, question: str, query_vector: Sequence[float], depth: int
) -> list[RankedRow]:
    vector = await vector_rank(session, query_vector, depth)
    keyword = await keyword_rank(session, question, depth)
    return fuse([vector, keyword], depth)

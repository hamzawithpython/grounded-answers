"""
Scoring for retrieval (docs/EVAL.md, "Method").

A retrieved chunk is a hit for an expected pair when all of these hold:
- it comes from the expected document;
- it comes from the document's current version (a hit from a retired version is a miss, by the owner's rule);
- the pair names a version, and the chunk is from that version;
- the expected clause label is one of the chunk's labels, as a whole label ("3.2" does not match "3.2.2").

Recall@k is the share of pairs with a hit in the top k. MRR is the mean of 1 / (rank of the first hit), 0 when
there is none. Both are over pairs, so a question with two sources counts twice.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from grounded_answers.evaluation.questions import ExpectedPair


@dataclass(frozen=True)
class ScoredChunk:
    document: str
    version: str
    position: int
    clause_labels: frozenset[str]
    score: float


@dataclass(frozen=True)
class Metrics:
    pairs: int
    recall_at_k: float
    mrr: float


def is_hit(pair: ExpectedPair, chunk: ScoredChunk, current_versions: Mapping[str, str]) -> bool:
    if chunk.document != pair.document:
        return False
    if chunk.version != current_versions.get(pair.document):
        return False
    if pair.version is not None and chunk.version != pair.version:
        return False
    return pair.clause in chunk.clause_labels


def first_hit_rank(
    pair: ExpectedPair, ranked: Sequence[ScoredChunk], current_versions: Mapping[str, str]
) -> int | None:
    """The 1-based rank of the first hit in the ranked list, or None."""
    for rank, chunk in enumerate(ranked, start=1):
        if is_hit(pair, chunk, current_versions):
            return rank
    return None


def metrics(ranks: Sequence[int | None], k: int) -> Metrics:
    if not ranks:
        return Metrics(0, 0.0, 0.0)
    hits = sum(1 for rank in ranks if rank is not None and rank <= k)
    reciprocal = sum(1.0 / rank for rank in ranks if rank is not None)
    return Metrics(len(ranks), hits / len(ranks), reciprocal / len(ranks))


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile, p in (0, 100]."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def separation(answerable: Sequence[float], unanswerable: Sequence[float]) -> float:
    """The chance that a random answerable score is above a random unanswerable one (ties count half).

    1.0 means the two groups never overlap, 0.5 means the score says nothing."""
    if not answerable or not unanswerable:
        raise ValueError("both groups need at least one score")
    wins = sum(1.0 if a > u else 0.5 if a == u else 0.0 for a in answerable for u in unanswerable)
    return wins / (len(answerable) * len(unanswerable))

"""
Run the question set through the three rankers as app_user and score the result (Session 5, Step 1).

One tenant transaction per ranker call, like one request. Latency is the time of the ranker call inside an open
transaction: it leaves out the connection and the set_config statement, and it covers a few hundred rows, so
it says nothing about scale. The query embedding is timed separately.

The report holds numbers and ids only. It never holds document text, and the question text is not copied into
the JSON (question ids are).
"""

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any, Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from grounded_answers.evaluation.questions import EvalQuestion
from grounded_answers.evaluation.rankers import (
    RankedRow,
    hybrid_rank,
    keyword_rank,
    vector_rank,
)
from grounded_answers.evaluation.scoring import (
    Metrics,
    ScoredChunk,
    first_hit_rank,
    is_hit,
    metrics,
    percentile,
    separation,
)
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction

RANKERS: Final = ("vector", "keyword", "hybrid")
DEFAULT_K: Final = 5
# Rows fetched from each list before fusion and scoring. MRR is over this many ranks.
DEFAULT_DEPTH: Final = 50

_VISIBLE_COUNTS = text("SELECT version_id, count(*) FROM grounded.chunks GROUP BY version_id")


class QueryEmbedder(Protocol):
    def query_embedding(self, text: str) -> list[float]: ...


class UnknownVersionError(RuntimeError):
    """A ranker returned a chunk of a version the loader did not register."""


@dataclass(frozen=True)
class LoadedVersion:
    """One uploaded version. The identity (document, version) comes from the document's header text."""

    version_id: uuid.UUID
    document: str
    version: str
    current: bool
    chunks_total: int


@dataclass(frozen=True)
class PairResult:
    question_id: str
    question_type: str
    document: str
    clause: str
    version: str | None
    rank: int | None


@dataclass(frozen=True)
class QuestionScores:
    """Scores of the top k chunks of one question for one ranker."""

    question_id: str
    answerable: bool
    top: tuple[float, ...]
    hit_scores: tuple[float, ...]
    returned: int


@dataclass(frozen=True)
class RankerReport:
    name: str
    overall: Metrics
    by_type: Mapping[str, Metrics]
    latency_p50_ms: float
    latency_p95_ms: float
    pair_results: tuple[PairResult, ...]
    scores: tuple[QuestionScores, ...]
    # Question ids for which the ranker returned fewer than k rows.
    shortfall: tuple[str, ...]


@dataclass(frozen=True)
class EvalReport:
    k: int
    depth: int
    questions: int
    answerable: int
    pairs: int
    rankers: Mapping[str, RankerReport]
    embed_p50_ms: float
    embed_p95_ms: float
    versions: tuple[LoadedVersion, ...]
    visible_chunks: Mapping[uuid.UUID, int]
    # Chunks returned to app_user from a version that is not current. Must be 0.
    retired_chunks_returned: int


def _to_scored(
    rows: Sequence[RankedRow], versions: Mapping[uuid.UUID, LoadedVersion]
) -> list[ScoredChunk]:
    scored: list[ScoredChunk] = []
    for row in rows:
        loaded = versions.get(row.version_id)
        if loaded is None:
            raise UnknownVersionError("a ranker returned a chunk of an unregistered version")
        scored.append(
            ScoredChunk(
                loaded.document,
                loaded.version,
                row.position,
                frozenset(row.clause_labels),
                row.score,
            )
        )
    return scored


async def _visible_counts(
    factory: async_sessionmaker[AsyncSession], context: TenantContext
) -> dict[uuid.UUID, int]:
    async with begin_tenant_transaction(factory, context) as session:
        result = await session.execute(_VISIBLE_COUNTS)
        return {row[0]: int(row[1]) for row in result.all()}


async def run_retrieval_eval(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    context: TenantContext,
    questions: Sequence[EvalQuestion],
    loaded: Sequence[LoadedVersion],
    query_embedder: QueryEmbedder,
    k: int = DEFAULT_K,
    depth: int = DEFAULT_DEPTH,
) -> EvalReport:
    versions = {item.version_id: item for item in loaded}
    current = {item.document: item.version for item in loaded if item.current}
    ranked: dict[str, dict[str, list[ScoredChunk]]] = {name: {} for name in RANKERS}
    returned: dict[str, dict[str, int]] = {name: {} for name in RANKERS}
    latency: dict[str, list[float]] = {name: [] for name in RANKERS}
    embed_ms: list[float] = []
    retired_returned = 0

    for question in questions:
        started = time.perf_counter()
        vector = query_embedder.query_embedding(question.text)
        embed_ms.append((time.perf_counter() - started) * 1000)
        for name in RANKERS:
            async with begin_tenant_transaction(session_factory, context) as session:
                started = time.perf_counter()
                if name == "vector":
                    rows = await vector_rank(session, vector, depth)
                elif name == "keyword":
                    rows = await keyword_rank(session, question.text, depth)
                else:
                    rows = await hybrid_rank(session, question.text, vector, depth)
                latency[name].append((time.perf_counter() - started) * 1000)
            retired_returned += sum(
                1
                for row in rows
                if row.version_id not in versions or not versions[row.version_id].current
            )
            returned[name][question.id] = len(rows)
            ranked[name][question.id] = _to_scored(rows, versions)

    reports = {
        name: _ranker_report(
            name, questions, ranked[name], returned[name], latency[name], current, k
        )
        for name in RANKERS
    }
    return EvalReport(
        k=k,
        depth=depth,
        questions=len(questions),
        answerable=sum(1 for q in questions if q.answerable),
        pairs=sum(len(q.pairs) for q in questions),
        rankers=reports,
        embed_p50_ms=percentile(embed_ms, 50),
        embed_p95_ms=percentile(embed_ms, 95),
        versions=tuple(loaded),
        visible_chunks=await _visible_counts(session_factory, context),
        retired_chunks_returned=retired_returned,
    )


def _ranker_report(
    name: str,
    questions: Sequence[EvalQuestion],
    ranked: Mapping[str, Sequence[ScoredChunk]],
    returned: Mapping[str, int],
    latency: Sequence[float],
    current: Mapping[str, str],
    k: int,
) -> RankerReport:
    pair_results: list[PairResult] = []
    scores: list[QuestionScores] = []
    for question in questions:
        chunks = ranked[question.id]
        top = chunks[:k]
        hit_scores: list[float] = []
        for pair in question.pairs:
            pair_results.append(
                PairResult(
                    question.id,
                    question.type,
                    pair.document,
                    pair.clause,
                    pair.version,
                    first_hit_rank(pair, chunks, current),
                )
            )
            hit_scores.extend(c.score for c in top if is_hit(pair, c, current))
        scores.append(
            QuestionScores(
                question.id,
                question.answerable,
                tuple(c.score for c in top),
                tuple(dict.fromkeys(hit_scores)),
                returned[question.id],
            )
        )
    types = sorted({r.question_type for r in pair_results})
    return RankerReport(
        name=name,
        overall=metrics([r.rank for r in pair_results], k),
        by_type={
            t: metrics([r.rank for r in pair_results if r.question_type == t], k) for t in types
        },
        latency_p50_ms=percentile(latency, 50),
        latency_p95_ms=percentile(latency, 95),
        pair_results=tuple(pair_results),
        scores=tuple(scores),
        shortfall=tuple(q.question_id for q in scores if q.returned < k),
    )


def _spread(values: Sequence[float]) -> str:
    if not values:
        return "n/a"
    return f"{min(values):.3f} / {median(values):.3f} / {max(values):.3f}"


def render_markdown(report: EvalReport) -> str:
    """Tables in the style of docs/EVAL.md."""
    k = report.k
    lines = [
        f"Retrieval evaluation: {report.questions} questions ({report.answerable} answerable, "
        f"{report.pairs} expected pairs), k = {k}, depth = {report.depth}.",
        "",
        f"| Ranker | R@{k} | MRR | p50 ms | p95 ms | Questions with fewer than {k} rows |",
        "|---|---|---|---|---|---|",
    ]
    for name, ranker in report.rankers.items():
        lines.append(
            f"| {name} | {ranker.overall.recall_at_k:.3f} | {ranker.overall.mrr:.3f} | "
            f"{ranker.latency_p50_ms:.1f} | {ranker.latency_p95_ms:.1f} | {len(ranker.shortfall)} |"
        )
    lines += [
        "",
        f"Query embedding: p50 {report.embed_p50_ms:.1f} ms, p95 {report.embed_p95_ms:.1f} ms.",
        f"Chunks of a non-current version returned to app_user: {report.retired_chunks_returned}.",
        "",
        f"By question type (R@{k} / MRR, pairs):",
        "",
        "| Type | " + " | ".join(report.rankers) + " |",
        "|---|" + "---|" * len(report.rankers),
    ]
    types = sorted({t for r in report.rankers.values() for t in r.by_type})
    for kind in types:
        cells = []
        for ranker in report.rankers.values():
            m = ranker.by_type[kind]
            cells.append(f"{m.recall_at_k:.2f} / {m.mrr:.3f} ({m.pairs})")
        lines.append(f"| {kind} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Score distributions (min / median / max). Scores are not comparable across rankers.",
        "",
        "| Ranker | Answerable top-1 | Scores of hits | Unanswerable top-1 | Separation |",
        "|---|---|---|---|---|",
    ]
    for name, ranker in report.rankers.items():
        top1_a = [s.top[0] for s in ranker.scores if s.answerable and s.top]
        top1_u = [s.top[0] for s in ranker.scores if not s.answerable and s.top]
        hits = [score for s in ranker.scores if s.answerable for score in s.hit_scores]
        sep = f"{separation(top1_a, top1_u):.3f}" if top1_a and top1_u else "n/a"
        lines.append(
            f"| {name} | {_spread(top1_a)} | {_spread(hits)} | {_spread(top1_u)} | {sep} |"
        )
    lines += [
        "",
        "| Document | Version | Current | Chunks | Visible to app_user |",
        "|---|---|---|---|---|",
    ]
    for item in sorted(report.versions, key=lambda v: (v.document, v.version)):
        lines.append(
            f"| {item.document} | {item.version} | {'yes' if item.current else 'no'} | "
            f"{item.chunks_total} | {report.visible_chunks.get(item.version_id, 0)} |"
        )
    total = sum(v.chunks_total for v in report.versions)
    visible = sum(report.visible_chunks.values())
    lines.append(f"| total | | | {total} | {visible} |")
    for name, ranker in report.rankers.items():
        misses = [r for r in ranker.pair_results if r.rank is None or r.rank > k]
        if misses:
            listed = ", ".join(f"{r.question_id}:{r.document} {r.clause}" for r in misses)
            lines += ["", f"Missed pairs, {name} ({len(misses)}): {listed}"]
    return "\n".join(lines)


def to_json(report: EvalReport) -> dict[str, Any]:
    """Numbers and ids only: the full per-question detail for later analysis."""
    return {
        "k": report.k,
        "depth": report.depth,
        "questions": report.questions,
        "pairs": report.pairs,
        "embed_ms": {"p50": report.embed_p50_ms, "p95": report.embed_p95_ms},
        "retired_chunks_returned": report.retired_chunks_returned,
        "versions": [
            {
                "document": v.document,
                "version": v.version,
                "current": v.current,
                "chunks_total": v.chunks_total,
                "visible": report.visible_chunks.get(v.version_id, 0),
            }
            for v in report.versions
        ],
        "rankers": {
            name: {
                "recall_at_k": r.overall.recall_at_k,
                "mrr": r.overall.mrr,
                "latency_ms": {"p50": r.latency_p50_ms, "p95": r.latency_p95_ms},
                "shortfall": list(r.shortfall),
                "pairs": [
                    {
                        "question": p.question_id,
                        "type": p.question_type,
                        "document": p.document,
                        "clause": p.clause,
                        "version": p.version,
                        "rank": p.rank,
                    }
                    for p in r.pair_results
                ],
                "scores": [
                    {
                        "question": s.question_id,
                        "answerable": s.answerable,
                        "top": list(s.top),
                        "hit_scores": list(s.hit_scores),
                        "returned": s.returned,
                    }
                    for s in r.scores
                ],
            }
            for name, r in report.rankers.items()
        },
    }

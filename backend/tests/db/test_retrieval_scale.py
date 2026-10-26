"""
Why there is no vector index (ADR 0030, migration 0016): the evidence, reproducible. Runs on demand, outside CI:

    uv run pytest -m eval -s -k scale

Needs the same environment as `pytest -m db` plus TEST_EMBEDDING_MODEL_DIR (the first test embeds the demo set once;
later tests reuse those vectors). About 20 minutes in all. Set EVAL_REPORT_PATH to a folder to also write each result
as JSON. These tests build their own data and put the schema back afterwards. They never skip.

Data. Tenant "real" holds the eight demo documents with their real vectors (the retired v1.0 of the expense policy
included). The other tenants hold noisy copies of the same chunks, a retired and a current version of each document: a
hard case, because policy corpora repeat each other (versions, boilerplate). NOISE sets how far a copy is from its
source (a noise norm of 0.45 is a cosine of about 0.91 to the source; 0.15 is about 0.99). The queries are the 50
questions of eval-questions.json, asked as app_user for the tenant under test.

Test 1 grows one tenant from 2,000 to 20,000 chunks among 20 tenants and asks, at each size, which plan the planner
picks for the tenant's vector query and what comes back, with the HNSW index present and without it.

Test 2 removes the primary key, so that the HNSW index is the only way to narrow, and sweeps pgvector's settings
(ef_search, iterative scans) on near-duplicate content.

A pitfall found on the way: psycopg prepares a statement it runs often, and a prepared plan ignores planner settings
changed later (enable_seqscan and so on). Every configuration here uses its own SQL text.
"""

import json
import os
import re
import statistics
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from grounded_answers.embedding.model import PinnedEmbedder
from grounded_answers.evaluation.questions import EvalQuestion, load_questions
from grounded_answers.evaluation.scoring import ScoredChunk, first_hit_rank, percentile
from grounded_answers.retrieval.rankers import vector_literal
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction
from tests.db.database import DbHandle

from .chunk_support import ChunkSpec, VersionSpec, as_floats, insert_document, vector_text
from .eval_support import EvalTenant, app_sessions, fresh_tenant, load_files, read_demo_files
from .ingest_support import Made

pytestmark = [pytest.mark.eval, pytest.mark.anyio]

DEMO = Path(__file__).resolve().parents[3] / "demo-data" / "indus-freight"
OTHER_TENANTS = 19
DOCS_PER_OTHER_TENANT = 10
GROWTH_SIZES = (2000, 5000, 10000, 20000)
DEPTH = 50

_VECTOR_SQL = """SELECT c.version_id, c.position,
  (c.embedding OPERATOR(extensions.<=>) CAST(:vector AS extensions.vector)) AS distance
  FROM grounded.chunks AS c
  ORDER BY distance, md5(c.body), c.version_id, c.position LIMIT :depth"""
# pgvector's own advice for relaxed_order: take the approximate rows in a materialised step, then sort them.
_RELAXED_SQL = """WITH relaxed AS MATERIALIZED (
  SELECT c.version_id, c.position, md5(c.body) AS tie_key,
  (c.embedding OPERATOR(extensions.<=>) CAST(:vector AS extensions.vector)) AS distance
  FROM grounded.chunks AS c ORDER BY distance LIMIT :depth)
  SELECT version_id, position, distance FROM relaxed
  ORDER BY distance + 0, tie_key, version_id, position"""
_EXACT = {"enable_indexscan": "off", "enable_bitmapscan": "off"}
_INDEX_ONLY = "enable_seqscan"


@dataclass(frozen=True)
class RealDocument:
    document: str
    # (version label, is current, chunks), oldest first
    versions: list[tuple[str, bool, list[ChunkSpec]]]


@dataclass(frozen=True)
class RealCorpus:
    documents: list[RealDocument]
    questions: list[EvalQuestion]
    query_vectors: list[list[float]]


_CORPUS: list[RealCorpus] = []  # filled by the first test, reused by the rest of the session


def _version_key(label: str) -> tuple[int, ...]:
    return tuple(int(part) for part in label.split("."))


async def real_corpus(handle: DbHandle, admin: psycopg.Connection, made: Made) -> RealCorpus:
    """The demo set through the real pipeline with the real model, once. Later calls reuse the vectors."""
    if _CORPUS:
        return _CORPUS[0]
    raw = os.environ.get("TEST_EMBEDDING_MODEL_DIR")
    if not raw:
        raise RuntimeError(
            "TEST_EMBEDDING_MODEL_DIR is not set (docs/RUNBOOK.md, 'Embedding model files'). "
            "This test never skips (ADR 0006)."
        )
    embedder = PinnedEmbedder(Path(raw))
    questions = load_questions(DEMO / "eval-questions.json")
    tenant = fresh_tenant(admin)
    loaded = await load_files(
        handle, admin, made, tenant, read_demo_files(DEMO / "upload"), embedder
    )
    documents: dict[str, list[tuple[str, bool, list[ChunkSpec]]]] = {}
    for version in sorted(loaded, key=lambda v: (v.document, _version_key(v.version))):
        rows = admin.execute(
            "SELECT section_number, heading, clause_labels, body, embedding::text FROM grounded.chunks "
            "WHERE version_id = %s ORDER BY position",
            (version.version_id,),
        ).fetchall()
        chunks = [ChunkSpec(r[3], r[4], r[0], r[1], tuple(r[2])) for r in rows]
        documents.setdefault(version.document, []).append(
            (version.version, version.current, chunks)
        )
    corpus = RealCorpus(
        [RealDocument(name, versions) for name, versions in documents.items()],
        questions,
        [embedder.query_embedding(q.text) for q in questions],
    )
    _CORPUS.append(corpus)
    return corpus


def noisy_copy(
    chunks: Sequence[ChunkSpec], rng: np.random.Generator, norm: float
) -> list[ChunkSpec]:
    copies = []
    for chunk in chunks:
        base = np.array(as_floats(chunk.vector))
        noise = rng.normal(size=base.shape)
        noise *= norm / np.linalg.norm(noise)
        moved = base + noise
        moved /= np.linalg.norm(moved)
        copies.append(
            ChunkSpec(
                chunk.body,
                vector_text(moved),
                chunk.section_number,
                chunk.heading,
                chunk.clause_labels,
            )
        )
    return copies


def current_chunks(corpus: RealCorpus) -> list[list[ChunkSpec]]:
    return [next(c for _, current, c in d.versions if current) for d in corpus.documents]


def other_tenants(
    admin: psycopg.Connection,
    make_tenant: Callable[[], EvalTenant],
    corpus: RealCorpus,
    rng: np.random.Generator,
    noise: float,
) -> None:
    sources = current_chunks(corpus)
    for _ in range(OTHER_TENANTS):
        tenant = make_tenant()
        for n in range(DOCS_PER_OTHER_TENANT):
            source = sources[n % len(sources)]
            insert_document(
                admin,
                tenant,
                f"copy-{n}",
                [
                    VersionSpec(noisy_copy(source, rng, noise), status="retired", current=False),
                    VersionSpec(noisy_copy(source, rng, noise)),
                ],
            )


@dataclass
class Run:
    rows: list[list[tuple[uuid.UUID, int]]] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    plan: str = ""

    @property
    def scan(self) -> str:
        found = re.search(
            r"(Index Scan using \w+|Bitmap Index Scan on \w+|Seq Scan on chunks)", self.plan
        )
        return found.group(1) if found else "?"


async def run_queries(
    factory: async_sessionmaker[AsyncSession],
    context: TenantContext,
    vectors: Sequence[Sequence[float]],
    sql: str,
    settings: dict[str, str],
    label: str,
    passes: int = 3,
) -> Run:
    """Every query once per pass; the first pass warms the caches and is not timed. The plan comes from one
    EXPLAIN ANALYZE. The SQL text carries the label so no two configurations share a prepared plan."""
    statement = f"/* {label} */ {sql}"
    run = Run()
    for pass_number in range(passes):
        run.rows = []
        for vector in vectors:
            async with begin_tenant_transaction(factory, context) as session:
                for name, value in settings.items():
                    await session.execute(
                        text("SELECT set_config(:name, :value, true)"),
                        {"name": name, "value": value},
                    )
                params = {"vector": vector_literal(vector), "depth": DEPTH}
                started = time.perf_counter()
                result = await session.execute(text(statement), params)
                got = [(row[0], int(row[1])) for row in result.all()]
                elapsed = (time.perf_counter() - started) * 1000
                if pass_number == 0 and not run.plan:
                    explained = await session.execute(
                        text("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) " + statement), params
                    )
                    run.plan = " ; ".join(
                        re.sub(r"'\[[^\]]*\]'", "'<v>'", line[0].strip())
                        for line in explained.all()
                    )
            run.rows.append(got)
            if pass_number > 0:
                run.latencies_ms.append(elapsed)
    return run


def overlap(run: Run, exact: Run, n: int) -> float:
    return statistics.mean(
        len(set(a[:n]) & set(b[:n])) / n for a, b in zip(run.rows, exact.rows, strict=True)
    )


def report(name: str, payload: object) -> None:
    folder = os.environ.get("EVAL_REPORT_PATH")
    if folder:
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{name}.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")


@pytest.fixture
def schema_restored(admin: psycopg.Connection) -> Iterator[None]:
    """These tests change indexes. Put the schema back as the migrations leave it, whatever happens."""
    yield
    admin.execute("DROP INDEX IF EXISTS grounded.chunks_embedding_hnsw")
    has_pkey = admin.execute(
        "SELECT 1 FROM pg_constraint WHERE conname = 'chunks_pkey' AND conrelid = 'grounded.chunks'::regclass"
    ).fetchone()
    if has_pkey is None:
        admin.execute(
            "ALTER TABLE grounded.chunks ADD CONSTRAINT chunks_pkey PRIMARY KEY (tenant_id, version_id, position)"
        )


def create_hnsw(admin: psycopg.Connection) -> None:
    admin.execute(
        "CREATE INDEX chunks_embedding_hnsw ON grounded.chunks "
        "USING hnsw (embedding extensions.vector_cosine_ops)"
    )


@pytest.mark.parametrize("with_index", [True, False], ids=["with_hnsw_index", "without_index"])
async def test_planner_choice_as_one_tenant_grows(
    grounded_db: DbHandle,
    admin: psycopg.Connection,
    made: Made,
    tenants: Callable[[], EvalTenant],
    schema_restored: None,
    with_index: bool,
) -> None:
    corpus = await real_corpus(grounded_db, admin, made)
    rng = np.random.default_rng(7)
    if with_index:
        create_hnsw(admin)
    other_tenants(admin, tenants, corpus, rng, noise=0.3)

    big = tenants()
    context = TenantContext(big.tenant, big.employee)
    chunks_have, documents_made = 0, 0
    sources = current_chunks(corpus)
    table: list[dict[str, Any]] = []
    async with app_sessions(grounded_db) as factory:
        for size in GROWTH_SIZES:
            while chunks_have < size:
                source = sources[documents_made % len(sources)]
                insert_document(
                    admin, big, f"big-{documents_made}", [VersionSpec(noisy_copy(source, rng, 0.3))]
                )
                chunks_have += len(source)
                documents_made += 1
            admin.execute("ANALYZE grounded.chunks")
            exact = await run_queries(
                factory,
                context,
                corpus.query_vectors,
                _VECTOR_SQL,
                _EXACT,
                f"{with_index} {size} exact",
            )
            natural = await run_queries(
                factory,
                context,
                corpus.query_vectors,
                _VECTOR_SQL,
                {},
                f"{with_index} {size} natural",
            )
            row = {
                "tenant_chunks": chunks_have,
                "plan": natural.scan,
                "p50_ms": round(percentile(natural.latencies_ms, 50), 1),
                "p95_ms": round(percentile(natural.latencies_ms, 95), 1),
                "fewest_rows": min(len(r) for r in natural.rows),
                "queries_under_5_rows": sum(len(r) < 5 for r in natural.rows),
                "overlap_at_5_with_exact": round(overlap(natural, exact, 5), 3),
            }
            table.append(row)
            print(f"\nGROWTH index={with_index} {row}")
            if not with_index:
                assert row["fewest_rows"] == DEPTH, row
                assert row["overlap_at_5_with_exact"] == 1.0, row
                assert "hnsw" not in str(natural.plan), row
    report(f"growth_{'with' if with_index else 'without'}_hnsw", table)
    if with_index:
        # The evidence for dropping it: at the largest size the planner uses HNSW and the answer is no longer exact.
        last = table[-1]
        assert "chunks_embedding_hnsw" in str(last["plan"]), last
        assert last["overlap_at_5_with_exact"] < 1.0 or last["fewest_rows"] < DEPTH, last


@pytest.mark.parametrize("noise", [0.45, 0.15], ids=["moderate_overlap", "near_duplicates"])
async def test_hnsw_settings_on_content_that_other_tenants_repeat(
    grounded_db: DbHandle,
    admin: psycopg.Connection,
    made: Made,
    tenants: Callable[[], EvalTenant],
    schema_restored: None,
    noise: float,
) -> None:
    corpus = await real_corpus(grounded_db, admin, made)
    rng = np.random.default_rng(20261026)
    real = tenants()
    identity: dict[uuid.UUID, tuple[str, str]] = {}
    labels_of: dict[tuple[uuid.UUID, int], frozenset[str]] = {}
    current_versions: dict[str, str] = {}
    for document in corpus.documents:
        inserted = insert_document(
            admin,
            real,
            document.document,
            [
                VersionSpec(chunks, status="ready" if current else "retired", current=current)
                for _, current, chunks in document.versions
            ],
        )
        for version_id, (label, current, chunks) in zip(
            inserted.version_ids, document.versions, strict=True
        ):
            identity[version_id] = (document.document, label)
            for position, chunk in enumerate(chunks):
                labels_of[(version_id, position)] = frozenset(chunk.clause_labels)
            if current:
                current_versions[document.document] = label
    other_tenants(admin, tenants, corpus, rng, noise)
    # Without the primary key, the HNSW index is the only way to narrow the search.
    admin.execute("ALTER TABLE grounded.chunks DROP CONSTRAINT chunks_pkey")
    create_hnsw(admin)
    admin.execute("ANALYZE grounded.chunks")

    ef, iterative = "hnsw.ef_search", "hnsw.iterative_scan"
    configurations: dict[str, tuple[dict[str, str], str]] = {
        "exact": (_EXACT, _VECTOR_SQL),
        "default": ({}, _VECTOR_SQL),
        "ef 100": ({ef: "100"}, _VECTOR_SQL),
        "ef 200": ({ef: "200"}, _VECTOR_SQL),
        "ef 400": ({ef: "400"}, _VECTOR_SQL),
        "strict ef 40": ({iterative: "strict_order"}, _VECTOR_SQL),
        "strict ef 100": ({ef: "100", iterative: "strict_order"}, _VECTOR_SQL),
        "relaxed ef 40": ({iterative: "relaxed_order"}, _RELAXED_SQL),
        "relaxed ef 100": ({ef: "100", iterative: "relaxed_order"}, _RELAXED_SQL),
    }
    context = TenantContext(real.tenant, real.employee)
    pairs = [(q, p) for q in corpus.questions for p in q.pairs]
    question_index = {q.id: i for i, q in enumerate(corpus.questions)}
    results: dict[str, Run] = {}
    async with app_sessions(grounded_db) as factory:
        for label, (settings, sql) in configurations.items():
            results[label] = await run_queries(
                factory, context, corpus.query_vectors, sql, settings, f"{noise} {label}"
            )

    table = []  # type: list[dict[str, Any]]
    for label, run in results.items():
        ranks = [
            first_hit_rank(
                pair,
                [
                    ScoredChunk(*identity[version], pos, labels_of[(version, pos)], 0.0)
                    for version, pos in run.rows[question_index[q.id]]
                ],
                current_versions,
            )
            for q, pair in pairs
        ]
        row: dict[str, Any] = {
            "noise": noise,
            "config": label,
            "scan": run.scan,
            "fewest_rows": min(len(r) for r in run.rows),
            "queries_under_5_rows": sum(len(r) < 5 for r in run.rows),
            "overlap_at_5_with_exact": round(overlap(run, results["exact"], 5), 3),
            "p50_ms": round(percentile(run.latencies_ms, 50), 1),
            "p95_ms": round(percentile(run.latencies_ms, 95), 1),
            "pairs_with_a_hit_in_top_5": sum(1 for r in ranks if r is not None and r <= 5),
        }
        table.append(row)
        print(f"\nSETTINGS {row}")
    report(f"settings_noise_{noise}", table)

    by_label = {r["config"]: r for r in table}
    assert (
        by_label["exact"]["fewest_rows"] == DEPTH
        and by_label["exact"]["overlap_at_5_with_exact"] == 1.0
    )
    if noise == 0.15:
        # Near-duplicate content: default settings return too few rows for most queries, and no setting tried
        # fixes every query.
        assert by_label["default"]["queries_under_5_rows"] >= 25, by_label["default"]
        tuned = [r for r in table if r["config"] not in ("exact", "default")]
        assert min(r["queries_under_5_rows"] for r in tuned) > 0, tuned

"""
The retrieval evaluation harness against the real database, with a fake embedder (runs in CI, `-m db`).

It checks the plumbing, not the quality: the rankers' SQL runs as app_user, a replaced version's chunks are
invisible, every ranker returns k rows when it can, and the harness would notice a leak. The vectors are fake, so
vector and hybrid quality are not asserted here. The real model runs in test_retrieval_eval_full.py (`-m eval`).
"""

import psycopg
import pytest

from grounded_answers.evaluation.harness import run_retrieval_eval
from grounded_answers.evaluation.questions import EvalQuestion, ExpectedPair
from grounded_answers.tenant_db import TenantContext
from grounded_answers.upload.detect import Kind
from tests.db.database import DbHandle

from .eval_support import (
    DemoFile,
    admin_sessions,
    app_sessions,
    fresh_tenant,
    load_files,
)
from .ingest_support import FakeEmbedder, Made

pytestmark = [pytest.mark.db, pytest.mark.anyio]

DOCUMENT = "IFL-HR-001"


def _policy(version: str, extra: str) -> bytes:
    sections = "\n\n".join(
        f"## {n}. Topic {n}\n\n{n}.1 Rule {n} covers the filler subject number {n} only."
        for n in range(1, 8)
    )
    return (
        f"# HR Policy\n\n| Field | Detail |\n|---|---|\n| Document ID | {DOCUMENT} |\n"
        f"| Version | {version} |\n\n---\n\n"
        f"## 3. Probation\n\n3.2 Probation lasts six months for drivers. {extra}\n\n{sections}\n"
    ).encode()


QUESTIONS = [
    EvalQuestion(
        "s1", "How long is probation for drivers?", "fact", (ExpectedPair(DOCUMENT, "3.2", None),)
    ),
    EvalQuestion("s2", "obsoleteword", "unanswerable", ()),
]


async def test_the_harness_runs_as_app_user_and_never_sees_a_replaced_version(
    grounded_db: DbHandle,
    admin: psycopg.Connection,
    made: Made,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    tenant = fresh_tenant(admin)
    directory = tmp_path_factory.mktemp("policies")
    files = []
    for version, extra in (("1.0", "The obsoleteword applies."), ("2.0", "Reviewed in 2026.")):
        path = directory / f"hr_v{version}.md"
        path.write_bytes(_policy(version, extra))
        files.append(DemoFile(path, Kind.MD, DOCUMENT, version))
    loaded = await load_files(grounded_db, admin, made, tenant, files, FakeEmbedder())
    assert [(v.version, v.current) for v in loaded] == [("1.0", False), ("2.0", True)]
    assert all(v.chunks_total >= 6 for v in loaded)

    context = TenantContext(tenant.tenant, tenant.employee)
    async with app_sessions(grounded_db) as factory:
        report = await run_retrieval_eval(
            session_factory=factory,
            context=context,
            questions=QUESTIONS,
            loaded=loaded,
            query_embedder=FakeEmbedder(),
        )

    assert report.questions == 2 and report.answerable == 1 and report.pairs == 1
    assert report.retired_chunks_returned == 0
    old, new = loaded
    assert report.visible_chunks.get(old.version_id, 0) == 0
    assert report.visible_chunks.get(new.version_id, 0) == new.chunks_total
    # A vector query always has k rows to return at this size. The keyword query has none for a word that
    # only the retired version contains.
    assert report.rankers["vector"].shortfall == ()
    assert report.rankers["hybrid"].shortfall == ()
    assert "s2" in report.rankers["keyword"].shortfall
    # Keyword search is real full-text search, so a distinctive question finds its clause even with fake vectors.
    keyword_pair = report.rankers["keyword"].pair_results[0]
    assert keyword_pair.rank == 1
    for ranker in report.rankers.values():
        assert ranker.latency_p95_ms >= ranker.latency_p50_ms > 0
        assert len(ranker.scores) == 2


async def test_the_harness_notices_a_ranker_that_returns_a_retired_version(
    grounded_db: DbHandle,
    admin: psycopg.Connection,
    made: Made,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Negative control: the same run as the cluster admin, which bypasses row-level security, must be flagged."""
    tenant = fresh_tenant(admin)
    directory = tmp_path_factory.mktemp("policies_control")
    files = []
    for version, extra in (("1.0", "The obsoleteword applies."), ("2.0", "Reviewed in 2026.")):
        path = directory / f"hr_v{version}.md"
        path.write_bytes(_policy(version, extra))
        files.append(DemoFile(path, Kind.MD, DOCUMENT, version))
    loaded = await load_files(grounded_db, admin, made, tenant, files, FakeEmbedder())

    async with admin_sessions(grounded_db) as factory:
        report = await run_retrieval_eval(
            session_factory=factory,
            context=TenantContext(tenant.tenant, tenant.employee),
            questions=QUESTIONS,
            loaded=loaded,
            query_embedder=FakeEmbedder(),
        )
    assert report.retired_chunks_returned > 0

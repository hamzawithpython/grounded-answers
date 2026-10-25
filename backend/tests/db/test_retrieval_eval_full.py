"""
The full retrieval evaluation (Session 5, Step 1): the eight demo documents through the real pipeline with the
real pinned model, then the 50 questions as app_user. Run on demand, outside CI:

    uv run pytest -m eval -s

Needs the same environment as `pytest -m db` (RUNBOOK, "Running Database Tests") and TEST_EMBEDDING_MODEL_DIR.
Set EVAL_REPORT_PATH to also write the per-question detail as JSON (numbers and ids only). It never skips: a missing
model directory is a failure (ADR 0006).
"""

import json
import os
from pathlib import Path

import psycopg
import pytest

from grounded_answers.embedding.model import PinnedEmbedder
from grounded_answers.evaluation.harness import render_markdown, run_retrieval_eval, to_json
from grounded_answers.evaluation.questions import load_questions
from grounded_answers.tenant_db import TenantContext
from tests.db.database import DbHandle

from .eval_support import app_sessions, fresh_tenant, load_files, read_demo_files
from .ingest_support import Made

pytestmark = [pytest.mark.eval, pytest.mark.anyio]

DEMO = Path(__file__).resolve().parents[3] / "demo-data" / "indus-freight"


async def test_retrieval_on_the_demo_set(
    grounded_db: DbHandle, admin: psycopg.Connection, made: Made
) -> None:
    raw = os.environ.get("TEST_EMBEDDING_MODEL_DIR")
    if not raw:
        raise RuntimeError(
            "TEST_EMBEDDING_MODEL_DIR is not set (docs/RUNBOOK.md, 'Embedding model files'). "
            "This test never skips (ADR 0006)."
        )
    embedder = PinnedEmbedder(Path(raw))
    questions = load_questions(DEMO / "eval-questions.json")
    files = read_demo_files(DEMO / "upload")
    assert [(f.document, f.version) for f in files if f.document == "IFL-FIN-007"] == [
        ("IFL-FIN-007", "1.0"),
        ("IFL-FIN-007", "2.0"),
    ]

    tenant = fresh_tenant(admin)
    loaded = await load_files(grounded_db, admin, made, tenant, files, embedder)

    async with app_sessions(grounded_db) as factory:
        report = await run_retrieval_eval(
            session_factory=factory,
            context=TenantContext(tenant.tenant, tenant.employee),
            questions=questions,
            loaded=loaded,
            query_embedder=embedder,
        )

    print("\n" + render_markdown(report))
    target = os.environ.get("EVAL_REPORT_PATH")
    if target:
        Path(target).write_text(json.dumps(to_json(report), indent=2), encoding="utf-8")

    assert (report.questions, report.answerable, report.pairs) == (50, 40, 45)
    assert report.retired_chunks_returned == 0
    assert report.rankers["vector"].shortfall == ()
    assert report.rankers["hybrid"].shortfall == ()
    # app_user sees exactly the chunks of the current versions.
    current_total = sum(v.chunks_total for v in loaded if v.current)
    assert sum(report.visible_chunks.values()) == current_total
    assert {v.document for v in loaded if not v.current} == {"IFL-FIN-007"}

"""
The provider test (Session 5, Step 3b, ADR 0031): the 50 questions, each with its retrieved sources, sent to the live
free-tier providers. It measures whether each provider works, what it costs (at list price) and how fast it is. It does
not judge answers (Steps 4 and 7). Run on demand, outside CI, with FICTIONAL data only:

    $env:EVAL_ANSWER_LIMIT = "5"          # questions per provider; default 5. Use 50 for the full set.
    uv run pytest -m eval -s -k answer_providers

Needs the same environment as `pytest -m db` and TEST_EMBEDDING_MODEL_DIR (docs/RUNBOOK.md), and the keys in
backend/.env (GROQ_API_KEY, GEMINI_API_KEY). Settings:

    EVAL_ANSWER_PROVIDERS   default "groq,gemini"
    EVAL_GROQ_PACE_SECONDS  default 12  (Groq's free limit is 8,000 tokens per minute)
    EVAL_GEMINI_PACE_SECONDS default 5
    EVAL_TOKEN_BUDGET       default 150000 tokens per provider (Groq's free limit is 200,000 per day)
    EVAL_REPORT_PATH        a file; the per-call numbers as JSON (no questions, sources, answers or keys)

A missing key for a requested provider is a failure, never a skip (ADR 0006). The test never prints a key.
"""

import json
import os
from pathlib import Path

import psycopg
import pytest

from grounded_answers.answering import AnswerModel
from grounded_answers.answering.adapters import GeminiAnswerModel, GroqAnswerModel
from grounded_answers.answering.transport import make_client
from grounded_answers.config import get_settings
from grounded_answers.embedding.model import PinnedEmbedder
from grounded_answers.evaluation.answer_run import (
    AnswerItem,
    ProviderRun,
    render_markdown,
    run_provider,
    select_items,
    to_json,
)
from grounded_answers.evaluation.questions import load_questions
from grounded_answers.retrieval import retrieve
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction
from tests.db.database import DbHandle

from .eval_support import app_sessions, fresh_tenant, load_files, read_demo_files
from .ingest_support import Made

pytestmark = [pytest.mark.eval, pytest.mark.anyio]

DEMO = Path(__file__).resolve().parents[3] / "demo-data" / "indus-freight"
DEFAULT_PACE = {"groq": 12.0, "gemini": 5.0}


async def test_answer_providers_on_the_demo_questions(
    grounded_db: DbHandle, admin: psycopg.Connection, made: Made
) -> None:
    raw = os.environ.get("TEST_EMBEDDING_MODEL_DIR")
    if not raw:
        raise RuntimeError(
            "TEST_EMBEDDING_MODEL_DIR is not set (docs/RUNBOOK.md). This test never skips."
        )
    wanted = [p.strip() for p in os.environ.get("EVAL_ANSWER_PROVIDERS", "groq,gemini").split(",")]
    limit = int(os.environ.get("EVAL_ANSWER_LIMIT", "5"))
    budget = int(os.environ.get("EVAL_TOKEN_BUDGET", "150000"))

    settings = get_settings()
    client = make_client()
    models: list[AnswerModel] = []
    for name in wanted:
        key = {"groq": settings.groq_api_key, "gemini": settings.gemini_api_key}.get(name)
        if key is None:
            raise RuntimeError(f"no API key for {name!r} in backend/.env (or the name is unknown)")
        if name == "groq":
            models.append(GroqAnswerModel(key, client))
        else:
            # gemini-2.5-flash-lite is closed to new accounts (Step 3b), so the model is a setting.
            models.append(
                GeminiAnswerModel(
                    key, client, model=os.environ.get("EVAL_GEMINI_MODEL", "gemini-3.5-flash-lite")
                )
            )

    embedder = PinnedEmbedder(Path(raw))
    questions = load_questions(DEMO / "eval-questions.json")
    tenant = fresh_tenant(admin)
    await load_files(grounded_db, admin, made, tenant, read_demo_files(DEMO / "upload"), embedder)

    items: list[AnswerItem] = []
    async with app_sessions(grounded_db) as factory:
        for question in questions:
            vector = embedder.query_embedding(question.text)
            async with begin_tenant_transaction(
                factory, TenantContext(tenant.tenant, tenant.employee)
            ) as session:
                chunks = await retrieve(
                    session, question.text, vector, top_k=settings.retrieval_top_k
                )
            items.append(AnswerItem(question.id, question.text, question.answerable, tuple(chunks)))

    chosen = select_items(items, limit)
    runs: list[ProviderRun] = []
    try:
        for model in models:
            pace = float(
                os.environ.get(
                    f"EVAL_{model.provider.upper()}_PACE_SECONDS", DEFAULT_PACE[model.provider]
                )
            )
            runs.append(await run_provider(model, chosen, pace_seconds=pace, token_budget=budget))
    finally:
        await client.aclose()

    print("\n" + render_markdown(runs))
    target = os.environ.get("EVAL_REPORT_PATH")
    if target:
        Path(target).write_text(json.dumps(to_json(runs), indent=2), encoding="utf-8")

    # Failures are visible in the printed report first. A provider with no successful call fails the test.
    for run in runs:
        assert any(r.error is None for r in run.records), (
            f"{run.provider}: no call succeeded ({run.stopped})"
        )

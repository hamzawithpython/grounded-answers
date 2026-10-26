"""
The live check of POST /ask (Session 5, Step 5c-2, ADR 0033): the real endpoint, the real embedding model, the real
database, the 8 demo documents ingested through the real pipeline, and a real answer model, asked a few of the 50
evaluation questions. It shows what a user would get and what the metering table recorded. It is a smoke test of the
whole path, not a measurement of quality (Step 7).

    # Rehearsal: no network, no key, a deterministic fake model. Everything else is real.
    $env:EVAL_ASK_LIVE = "dry"
    uv run pytest -m eval -s -k ask_live

    # The live check: real Groq (fictional data only, free tier). Needs GROQ_API_KEY in backend/.env.
    $env:EVAL_ASK_LIVE = "groq"
    $env:EVAL_ASK_LIMIT = "5"            # questions; default 5, at most 10
    uv run pytest -m eval -s -k ask_live

Needs the same environment as `pytest -m db` and TEST_EMBEDDING_MODEL_DIR (docs/RUNBOOK.md). Settings:

    EVAL_ASK_LIVE             "dry" or "groq"; anything else fails (it never skips, and never calls a provider by accident)
    EVAL_ASK_LIMIT            questions to ask, default 5, at most 10
    EVAL_GROQ_PACE_SECONDS    default 12 (Groq's free limit is 8,000 tokens per minute)
    EVAL_REPORT_PATH          a file; the numbers as JSON

The report holds question ids, status codes, outcome codes, counts, tokens, cost and timings. It never holds a
question, a source, an answer or a key.
"""

import asyncio
import gc
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from grounded_answers.answering.adapters import GroqAnswerModel
from grounded_answers.answering.transport import make_client
from grounded_answers.ask.providers import ProviderRegistry
from grounded_answers.auth_keys import load_key_ring
from grounded_answers.auth_tokens import issue_access_token
from grounded_answers.config import get_settings
from grounded_answers.embedding.model import PinnedEmbedder
from grounded_answers.evaluation.answer_run import AnswerItem, select_items
from grounded_answers.evaluation.questions import load_questions
from grounded_answers.evaluation.scoring import percentile
from grounded_answers.main import create_app
from grounded_answers.runtime import selector_loop_factory
from tests.answering_support import answer_json, result_of
from tests.db.auth_support import SELECTOR, Accounts, flush_test_valkey
from tests.db.database import DbHandle
from tests.db.eval_support import fresh_tenant, load_files, read_demo_files
from tests.db.ingest_support import Made

pytestmark = [pytest.mark.eval, pytest.mark.live]

DEMO = Path(__file__).resolve().parents[3] / "demo-data" / "indus-freight"
MAX_QUESTIONS = 10


class DryModel:
    """A deterministic stand-in for the answer model: it refuses every question, with a plausible token count."""

    provider = "groq"
    model = "openai/gpt-oss-120b"

    async def answer(self, request: Any) -> Any:
        return result_of(
            answer_json(refused=True, reason="not_in_documents", answer=""),
            tokens_in=1800,
            tokens_out=120,
        )


def _mode() -> str:
    mode = os.environ.get("EVAL_ASK_LIVE", "")
    if mode not in {"dry", "groq"}:
        raise RuntimeError(
            'EVAL_ASK_LIVE must be "dry" (a fake model) or "groq" (the real provider). '
            "This test never skips and never calls a provider by accident (docs/RUNBOOK.md)."
        )
    return mode


def test_ask_live_on_the_demo_documents(
    grounded_db: DbHandle,
    admin: psycopg.Connection,
    made: Made,
    accounts: Accounts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mode = _mode()
    raw = os.environ.get("TEST_EMBEDDING_MODEL_DIR")
    if not raw:
        raise RuntimeError(
            "TEST_EMBEDDING_MODEL_DIR is not set (docs/RUNBOOK.md). This test never skips."
        )
    limit = min(int(os.environ.get("EVAL_ASK_LIMIT", "5")), MAX_QUESTIONS)
    pace = float(os.environ.get("EVAL_GROQ_PACE_SECONDS", "12"))
    if mode == "groq" and get_settings().groq_api_key is None:
        raise RuntimeError("GROQ_API_KEY is not set in backend/.env")

    # 1. Fictional data: a fresh tenant with the demo documents ingested through the real pipeline.
    tenant = fresh_tenant(admin)
    embedder = PinnedEmbedder(Path(raw))
    with asyncio.Runner(loop_factory=selector_loop_factory) as runner:
        runner.run(
            load_files(grounded_db, admin, made, tenant, read_demo_files(DEMO / "upload"), embedder)
        )
    del embedder  # the app loads its own copy; both at once would hold about 1.5 GB
    gc.collect()
    admin.execute(
        "INSERT INTO grounded.tenant_settings (tenant_id, answer_providers, data_class, daily_cost_cap_micro_usd) "
        "VALUES (%s, ARRAY['groq'], 'fictional', 50000)",
        (tenant.tenant,),
    )
    admin.commit()

    questions = load_questions(DEMO / "eval-questions.json")
    chosen = select_items([AnswerItem(q.id, q.text, q.answerable, ()) for q in questions], limit)
    texts = {q.id: q.text for q in questions}
    token = issue_access_token(
        ring=load_key_ring(accounts.key_path),
        user_id=tenant.employee,
        tenant_id=tenant.tenant,
        role_hint="employee",
    )

    # 2. The real app, with the real embedding model in the web process.
    monkeypatch.setenv("ASK_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_MODEL_DIR", raw)
    get_settings.cache_clear()
    flush_test_valkey()
    rows: list[dict[str, Any]] = []
    with TestClient(
        create_app(), raise_server_exceptions=False, backend_options=SELECTOR
    ) as client:
        app: Any = client.app
        started = time.monotonic()
        while app.state.query_embedder.state != "ready":
            assert app.state.query_embedder.state != "failed", "the embedding model did not load"
            assert time.monotonic() - started < 120, (
                "the embedding model took more than 2 minutes to load"
            )
            time.sleep(0.2)
        load_seconds = time.monotonic() - started

        deps = app.state.ask_deps
        http_client = None
        if mode == "dry":
            deps.providers = ProviderRegistry({"groq": DryModel()})
        else:
            http_client = make_client()
            groq_key = get_settings().groq_api_key
            assert groq_key is not None
            deps.providers = ProviderRegistry({"groq": GroqAnswerModel(groq_key, http_client)})
        app.state.ask_service = deps.service()

        try:
            for number, item in enumerate(chosen):
                if number and mode == "groq":
                    time.sleep(pace)  # Groq's free tier allows 8,000 tokens a minute
                begun = time.perf_counter()
                response = client.post(
                    "/ask",
                    json={"question": texts[item.question_id]},
                    headers={"Authorization": f"Bearer {token}"},
                )
                elapsed_ms = (time.perf_counter() - begun) * 1000
                body = response.json()
                rows.append(
                    {
                        "question_id": item.question_id,
                        "answerable": item.answerable,
                        "http": response.status_code,
                        "status": body.get("status") or body.get("code"),
                        "reason": body.get("reason"),
                        "citations": len(body.get("citations", [])),
                        "clause_kinds": sorted(
                            {c["clause_kind"] for c in body.get("citations", [])}
                        ),
                        "wall_ms": round(elapsed_ms),
                    }
                )
        finally:
            # http_client is left for the process to close: it was used on the app's event loop, not on this thread's.
            del http_client

    # 3. What the metering table recorded: numbers and codes only.
    with psycopg.connect(grounded_db.admin_test_url()) as conn:
        cursor = conn.execute(
            "SELECT outcome, provider, model, attempts, retrieved_count, input_tokens, output_tokens, "
            "cost_micro_usd, latency_ms, invalid_codes FROM grounded.answer_usage WHERE tenant_id = %s "
            "ORDER BY created_at, id",
            (tenant.tenant,),
        )
        names = [column.name for column in cursor.description or []]
        usage = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    print("\n## POST /ask, " + ("dry run (fake model)" if mode == "dry" else "live (Groq)"))
    print(
        f"\nEmbedding model loaded in the web process in {load_seconds:.1f} s. Questions asked: {len(rows)}.\n"
    )
    print("| Question | Answerable | HTTP | Result | Citations | Clause kinds | Wall ms |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        result = r["reason"] or r["status"]
        print(
            f"| {r['question_id']} | {r['answerable']} | {r['http']} | {result} | {r['citations']} | "
            f"{','.join(r['clause_kinds']) or '-'} | {r['wall_ms']} |"
        )
    print("\nMetering rows (answer_usage):\n")
    print(
        "| Outcome | Attempts | Retrieved | In tokens | Out tokens | Cost micro-USD | Latency ms | Invalid codes |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for u in usage:
        print(
            f"| {u['outcome']} | {u['attempts']} | {u['retrieved_count']} | {u['input_tokens']} | "
            f"{u['output_tokens']} | {u['cost_micro_usd']} | {u['latency_ms']} | {','.join(u['invalid_codes']) or '-'} |"
        )
    walls = [r["wall_ms"] for r in rows]
    total_tokens = sum(u["input_tokens"] + u["output_tokens"] for u in usage)
    total_cost = sum(u["cost_micro_usd"] for u in usage)
    print(
        f"\nTotals: {total_tokens} tokens, {total_cost} micro-USD (${total_cost / 1_000_000:.6f}); "
        f"wall time p50 {statistics.median(walls):.0f} ms, p95 {percentile([float(w) for w in walls], 95):.0f} ms."
    )

    target = os.environ.get("EVAL_REPORT_PATH")
    if target:
        Path(target).write_text(
            json.dumps({"mode": mode, "answers": rows, "usage": usage}, indent=2, default=str),
            encoding="utf-8",
        )

    # The path works end to end: nothing failed unexpectedly, and every answered question left a metering row.
    assert not any(r["http"] == 500 for r in rows), "an unexpected error"
    assert len(usage) >= sum(1 for r in rows if r["http"] == 200)

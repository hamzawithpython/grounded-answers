"""
A running app with POST /ask on, for the HTTP tests of Step 5c-2 (ADR 0033).

The app starts through its real lifespan: real database as app_user, real Valkey, real rate limiter, in-flight set,
metering flag, retrieval and metering writes. Only two things are replaced: the embedding model (a fake that returns a
fixed vector, so no model files are needed) and the answer model (a scripted fake registered in the provider
registry, so no network and no key). A fresh tenant with one document and one chunk is created for each test, with
settings for the Groq adapter on fictional data and a $0.05 daily cap.
"""

import asyncio
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from grounded_answers import main as main_module
from grounded_answers.answering.types import AnswerRequest, AnswerResult
from grounded_answers.ask.providers import ProviderRegistry
from grounded_answers.auth_keys import load_key_ring
from grounded_answers.auth_tokens import issue_access_token
from grounded_answers.config import get_settings
from grounded_answers.main import create_app
from tests.answering_support import answer_json, result_of
from tests.db.auth_support import SELECTOR, Accounts, flush_test_valkey
from tests.db.chunk_support import (
    ChunkSpec,
    VersionSpec,
    as_floats,
    axis_vector,
    insert_document,
    remove_tenant,
)
from tests.db.database import DbHandle
from tests.db.eval_support import EvalTenant, fresh_tenant

QUOTE = "Notice is thirty days."
CHUNK_BODY = f"4.1 {QUOTE} Leave is paid."
GOOD_REPLY = answer_json(
    answer="Notice is thirty days. [1]", citations=[{"ref": 1, "quote": QUOTE}]
)
VECTOR = as_floats(axis_vector({0: 1.0}))


class FakeEmbedModel:
    """The query model of the web process: one fixed 384-dimension vector."""

    def query_embedding(self, text: str) -> list[float]:
        return list(VECTOR)


class FakeAnswerModel:
    """Plays a script of results or errors, one per call (the last one repeats). Records every request."""

    provider = "groq"
    model = "openai/gpt-oss-120b"

    def __init__(self) -> None:
        self.script: list[AnswerResult | Exception] = []
        self.requests: list[AnswerRequest] = []
        self.delay = 0.0
        self.started = threading.Event()
        self._lock = threading.Lock()

    async def answer(self, request: AnswerRequest) -> AnswerResult:
        with self._lock:
            self.requests.append(request)
            step = (
                self.script[min(len(self.requests), len(self.script)) - 1] if self.script else None
            )
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(step, Exception):
            raise step
        return step if step is not None else result_of(GOOD_REPLY)

    @property
    def calls(self) -> int:
        return len(self.requests)


@dataclass
class AskWorld:
    client: TestClient
    handle: DbHandle
    accounts: Accounts
    tenant: EvalTenant
    employee_token: str
    admin_token: str
    document_id: uuid.UUID
    model: FakeAnswerModel
    emails: dict[str, str] = field(default_factory=dict)
    created: list[EvalTenant] = field(
        default_factory=list
    )  # every tenant this test made, removed at the end

    def post(
        self, question: str = "How many days of notice?", *, token: str | None = None, **json: Any
    ) -> Any:
        body: dict[str, Any] = {"question": question, **json}
        headers = {"Authorization": f"Bearer {token or self.employee_token}"}
        return self.client.post("/ask", json=body, headers=headers)

    def admin_rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.handle.admin_test_url()) as conn:
            return list(conn.execute(sql, params).fetchall())

    def usage(self, tenant: uuid.UUID | None = None) -> list[dict[str, Any]]:
        """The tenant's metering rows, oldest first, as dictionaries."""
        with psycopg.connect(self.handle.admin_test_url()) as conn:
            cursor = conn.execute(
                "SELECT provider, model, outcome, attempts, retrieved_count, input_tokens, output_tokens, "
                "cost_micro_usd, latency_ms, invalid_codes FROM grounded.answer_usage "
                "WHERE tenant_id = %s ORDER BY created_at, id",
                (tenant or self.tenant.tenant,),
            )
            names = [column.name for column in cursor.description or []]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def outcomes(self, tenant: uuid.UUID | None = None) -> list[str]:
        return [row["outcome"] for row in self.usage(tenant)]

    def set_settings(
        self,
        *,
        providers: Sequence[str] = ("groq",),
        data_class: str = "fictional",
        cap: int = 50_000,
        tenant: uuid.UUID | None = None,
    ) -> None:
        with psycopg.connect(self.handle.admin_test_url()) as conn:
            conn.execute(
                "INSERT INTO grounded.tenant_settings (tenant_id, answer_providers, data_class, "
                "daily_cost_cap_micro_usd) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (tenant_id) DO UPDATE SET answer_providers = EXCLUDED.answer_providers, "
                "data_class = EXCLUDED.data_class, "
                "daily_cost_cap_micro_usd = EXCLUDED.daily_cost_cap_micro_usd",
                (tenant or self.tenant.tenant, list(providers), data_class, cap),
            )

    def add_usage(self, cost: int, *, days_ago: int = 0, tenant: uuid.UUID | None = None) -> None:
        with psycopg.connect(self.handle.admin_test_url()) as conn:
            conn.execute(
                "INSERT INTO grounded.answer_usage (id, tenant_id, provider, model, outcome, attempts, "
                "retrieved_count, input_tokens, output_tokens, cost_micro_usd, latency_ms, created_at) "
                "VALUES (%s, %s, 'groq', 'm', 'answered', 1, 1, 1, 1, %s, 1, now() - make_interval(days => %s))",
                (uuid.uuid4(), tenant or self.tenant.tenant, cost, days_ago),
            )

    def new_tenant(
        self, *, with_document: bool = True, body: str = CHUNK_BODY
    ) -> tuple[EvalTenant, str]:
        """Another tenant with its own settings, document and an employee token."""
        with psycopg.connect(self.handle.admin_test_url()) as conn:
            other = fresh_tenant(conn)
            self.created.append(other)
            if with_document:
                _insert_handbook(conn, other, body)
        self.set_settings(tenant=other.tenant)
        token = issue_access_token(
            ring=load_key_ring(self.accounts.key_path),
            user_id=other.employee,
            tenant_id=other.tenant,
            role_hint="employee",
        )
        return other, token

    @property
    def deps(self) -> Any:
        return self.client.app.state.ask_deps  # type: ignore[attr-defined]

    def rebuild(self, **overrides: Any) -> None:
        """Replace parts of the running /ask service (a limiter with a fake clock, a failing store) and rebuild it."""
        for name, value in overrides.items():
            setattr(self.deps, name, value)
        self.client.app.state.ask_service = self.deps.service()  # type: ignore[attr-defined]


def _insert_handbook(
    conn: psycopg.Connection, tenant: EvalTenant, body: str = CHUNK_BODY
) -> uuid.UUID:
    inserted = insert_document(
        conn,
        tenant,
        "Handbook",
        [
            VersionSpec(
                chunks=[
                    ChunkSpec(
                        body=body,
                        vector=axis_vector({0: 1.0}),
                        section_number="4",
                        heading="Notice period",
                        clause_labels=("4.1",),
                    )
                ]
            )
        ],
    )
    return inserted.document_id


def _wait_until_ready(client: TestClient) -> None:
    deadline = time.monotonic() + 20
    while client.app.state.query_embedder.state != "ready":  # type: ignore[attr-defined]
        assert time.monotonic() < deadline, "the fake embedder never became ready"
        time.sleep(0.02)


@pytest.fixture
def ask_world(accounts: Accounts, monkeypatch: MonkeyPatch, tmp_path: Path) -> Iterator[AskWorld]:
    monkeypatch.setenv("ASK_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(
        main_module, "load_embedder", lambda directory, process="worker": FakeEmbedModel()
    )
    get_settings.cache_clear()
    flush_test_valkey()

    with psycopg.connect(accounts.handle.admin_test_url()) as conn:
        tenant = fresh_tenant(conn)
        document = _insert_handbook(conn, tenant)
    ring = load_key_ring(accounts.key_path)
    tokens = {
        role: issue_access_token(ring=ring, user_id=user, tenant_id=tenant.tenant, role_hint=role)
        for role, user in (("employee", tenant.employee), ("tenant_admin", tenant.admin))
    }
    model = FakeAnswerModel()
    with TestClient(
        create_app(), raise_server_exceptions=False, backend_options=SELECTOR
    ) as client:
        _wait_until_ready(client)
        # Never a real adapter: backend/.env may hold real provider keys, and Settings would build adapters from them.
        # The registry is replaced by one that holds only the fake, so no test can send a request to a provider.
        deps = client.app.state.ask_deps  # type: ignore[attr-defined]
        deps.providers = ProviderRegistry()
        deps.providers.register("groq", model)
        client.app.state.ask_service = deps.service()  # type: ignore[attr-defined]
        assert deps.providers.names == ("groq",)
        world = AskWorld(
            client,
            accounts.handle,
            accounts,
            tenant,
            tokens["employee"],
            tokens["tenant_admin"],
            document,
            model,
        )
        world.set_settings()
        world.created.append(tenant)
        try:
            yield world
        finally:
            # The rows were committed, so they stay in the shared test database. Tests that look at the whole database
            # as the cluster admin (the retrieval negative control) must not meet them.
            with psycopg.connect(accounts.handle.admin_test_url()) as conn:
                for made_tenant in world.created:
                    remove_tenant(conn, made_tenant)
    get_settings.cache_clear()

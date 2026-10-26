"""
The sentinel test through HTTP (ADR 0033, decision 8), with the real database and Valkey.

Known strings are put into the question, the document text, the document title and the model's answer, and known ids
are the tenant, the user and the email of the caller. Every path of POST /ask is then run. What must hold:

- no log record, at any level, from any logger, contains the question, the document text or the raw tenant or user
  ids or the email (and, unless the answer is meant to be shown, the answer or the title);
- no response contains them, except an answered response, which legitimately carries the answer, the title and the
  quote, and never the question or the document text outside the quote;
- no column of any metering row except its own keys holds them;
- no Valkey key holds an id, an email or a sentinel;
- the lines an operator needs do exist (the request id and the keyed hash of the tenant), so the test cannot pass by
  logging nothing.
"""

import logging
import threading
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
import redis
from redis.asyncio import Redis as AsyncRedis

from grounded_answers.answering.types import (
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from grounded_answers.ask.types import Caller, StoreUnavailable, TenantState, UsageRecord
from grounded_answers.ask.valkey import AskLimiter
from grounded_answers.embedding.query import QueryEmbedderNotReady
from grounded_answers.log_ref import tenant_log_ref
from tests.answering_support import answer_json, result_of
from tests.db.ask_http_support import AskWorld
from tests.db.auth_support import VALKEY_TEST_URL

pytestmark = pytest.mark.db

SENTINEL_Q = "SENTINEL-QUESTION-what-is-the-ceo-salary"
SENTINEL_C = "SENTINEL-CHUNK-confidential-clause"
SENTINEL_A = "SENTINEL-ANSWER-sentence"
SENTINEL_T = "SENTINEL-TITLE-Secret-Handbook"
QUOTE = "Notice is thirty days."
GOOD = answer_json(answer=f"{SENTINEL_A} {QUOTE} [1]", citations=[{"ref": 1, "quote": QUOTE}])
BAD = answer_json(answer=f"{SENTINEL_A} 99 days.", citations=[{"ref": 1, "quote": QUOTE}])
QUESTION = f"{SENTINEL_Q}?"


def _forms(value: uuid.UUID) -> list[str]:
    return [str(value), value.hex, str(value).upper()]


def _refusal(reason: str) -> Any:
    return result_of(answer_json(refused=True, reason=reason, answer=""))


class Scenario:
    """What to arrange and what to send. `shows_answer` marks the scenarios whose response may hold the answer."""

    def __init__(
        self,
        run: Callable[[AskWorld], Any],
        *,
        shows_answer: bool = False,
        metered: bool = True,
    ) -> None:
        self.run = run
        self.shows_answer = shows_answer
        self.metered = metered


def _post(world: AskWorld) -> Any:
    return world.post(QUESTION)


def _script(*steps: Any) -> Callable[[AskWorld], Any]:
    def run(world: AskWorld) -> Any:
        world.model.script = list(steps)
        return _post(world)

    return run


def _store_that(
    *, state: Exception | None = None, retrieve: Exception | None = None
) -> Callable[[AskWorld], Any]:
    def run(world: AskWorld) -> Any:
        inner = world.deps.store

        class Wrapped:
            async def read_state(self, caller: Caller) -> TenantState:
                if state is not None:
                    raise state
                return await inner.read_state(caller)  # type: ignore[no-any-return]

            async def retrieve(
                self, caller: Caller, question: str, vector: list[float], top_k: int
            ) -> Any:
                if retrieve is not None:
                    raise retrieve
                return await inner.retrieve(caller, question, vector, top_k)

            async def record(self, caller: Caller, record: UsageRecord) -> None:
                await inner.record(caller, record)

        world.rebuild(store=Wrapped())
        return _post(world)

    return run


def _rate_limited(world: AskWorld) -> Any:
    class Clock:
        def __call__(self) -> int:
            return 1_800_000_000_000

    cache = world.client.app.state.cache  # type: ignore[attr-defined]
    key = world.client.app.state.rate_limit_hmac_key  # type: ignore[attr-defined]
    world.rebuild(limiter=AskLimiter(cache, key, Clock(), tenant_limit=20, user_limit=1))
    _post(world)
    return _post(world)


def _cap(world: AskWorld) -> Any:
    world.set_settings(cap=10)
    world.add_usage(10)
    return _post(world)


def _not_configured(world: AskWorld) -> Any:
    world.set_settings(providers=())
    return _post(world)


def _disallowed(world: AskWorld) -> Any:
    with psycopg.connect(world.handle.admin_test_url()) as conn:
        conn.execute(
            "UPDATE grounded.tenant_settings SET data_class = 'real' WHERE tenant_id = %s",
            (world.tenant.tenant,),
        )
    return _post(world)


def _no_key(world: AskWorld) -> Any:
    world.set_settings(providers=("gemini",))
    return _post(world)


def _embedder_loading(world: AskWorld) -> Any:
    class Loading:
        async def embed(self, text: str) -> list[float]:
            raise QueryEmbedderNotReady(SENTINEL_Q)

    world.rebuild(embedder=Loading())
    return _post(world)


def _unauthenticated(world: AskWorld) -> Any:
    return world.client.post("/ask", json={"question": QUESTION})


def _invalid_body(world: AskWorld) -> Any:
    return world.client.post(
        "/ask",
        # An unknown field name is echoed in the error location (never a value): that is the caller's own key. The
        # values, here the question and a sentinel in the extra field, must not come back.
        json={"question": QUESTION, "tenant_id": SENTINEL_C},
        headers={"Authorization": f"Bearer {world.employee_token}"},
    )


def _suspended(world: AskWorld) -> Any:
    with psycopg.connect(world.handle.admin_test_url()) as conn:
        conn.execute(
            "UPDATE grounded.tenants SET status = 'suspended' WHERE id = %s", (world.tenant.tenant,)
        )
    return _post(world)


def _valkey_down(world: AskWorld) -> Any:
    dead = AsyncRedis.from_url(
        "redis://127.0.0.1:1/0", socket_timeout=0.5, socket_connect_timeout=0.5
    )
    key = world.client.app.state.rate_limit_hmac_key  # type: ignore[attr-defined]
    world.rebuild(
        limiter=AskLimiter(dead, key, lambda: 1_800_000_000_000, tenant_limit=20, user_limit=5)
    )
    return _post(world)


def _metering_down(world: AskWorld) -> Any:
    with psycopg.connect(world.handle.admin_test_url()) as conn:
        conn.execute("REVOKE INSERT ON grounded.answer_usage FROM app_user")
    try:
        first = _post(world)
        _post(world)  # refused by the flag
        return first
    finally:
        with psycopg.connect(world.handle.admin_test_url()) as conn:
            conn.execute("GRANT INSERT ON grounded.answer_usage TO app_user")


def _busy(world: AskWorld) -> Any:
    world.model.delay = 1.0
    results: list[Any] = []
    threads = [threading.Thread(target=lambda: results.append(_post(world))) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert world.model.started.wait(timeout=10)
    while world.model.calls < 2:
        threading.Event().wait(0.02)
    busy = _post(world)
    for thread in threads:
        thread.join()
    return busy


SCENARIOS: dict[str, Scenario] = {
    "answered": Scenario(_script(result_of(GOOD)), shows_answer=True),
    "retry_then_answered": Scenario(_script(result_of(BAD), result_of(GOOD)), shows_answer=True),
    "could_not_verify": Scenario(_script(result_of(BAD), result_of(BAD))),
    "refused_not_in_documents": Scenario(_script(_refusal("not_in_documents"))),
    "refused_needs_clarification": Scenario(_script(_refusal("needs_clarification"))),
    "refused_conflicting": Scenario(_script(_refusal("conflicting_sources"))),
    "provider_unavailable": Scenario(_script(ProviderUnavailable(SENTINEL_A))),
    "provider_rate_limited": Scenario(_script(ProviderRateLimited(SENTINEL_A, 0.05))),
    "provider_timeout": Scenario(_script(ProviderTimeout(SENTINEL_A))),
    "rate_limited": Scenario(_rate_limited),
    "daily_cap": Scenario(_cap, metered=False),
    "not_configured": Scenario(_not_configured),
    "provider_disallowed": Scenario(_disallowed),
    "no_key": Scenario(_no_key),
    "embedder_loading": Scenario(_embedder_loading),
    "state_down": Scenario(_store_that(state=StoreUnavailable(SENTINEL_Q)), metered=False),
    "retrieve_down": Scenario(_store_that(retrieve=StoreUnavailable(SENTINEL_C))),
    "unexpected": Scenario(
        _store_that(retrieve=RuntimeError(f"{SENTINEL_Q} {SENTINEL_C} {SENTINEL_A}"))
    ),
    "unauthenticated": Scenario(_unauthenticated, metered=False),
    "invalid_body": Scenario(_invalid_body, metered=False),
    "suspended_tenant": Scenario(_suspended, metered=False),
    "valkey_down": Scenario(_valkey_down, metered=False),
    # The metering insert is broken on purpose, so no row is expected: the answer is still served, and the flag is set.
    "metering_down": Scenario(_metering_down, shows_answer=True, metered=False),
    "busy": Scenario(_busy, metered=False),
}


def _prepare(world: AskWorld) -> None:
    """Put the sentinels where they belong: into the document text and title."""
    with psycopg.connect(world.handle.admin_test_url()) as conn:
        conn.execute(
            "UPDATE grounded.chunks SET body = %s WHERE tenant_id = %s",
            (f"4.1 {SENTINEL_C} {QUOTE}", world.tenant.tenant),
        )
        conn.execute(
            "UPDATE grounded.documents SET title = %s WHERE tenant_id = %s",
            (SENTINEL_T, world.tenant.tenant),
        )


def _record_text(record: logging.LogRecord) -> str:
    """Everything a log handler could write for this record: the message, the arguments, extras and the traceback."""
    formatted = logging.Formatter().format(record)
    plain = logging.LogRecord("", 0, "", 0, "", (), None).__dict__
    extras = {key: value for key, value in record.__dict__.items() if key not in plain}
    return f"{formatted} {record.args!r} {extras!r}"


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_no_scenario_leaks_into_logs_responses_rows_or_valkey(
    name: str, ask_world: AskWorld, caplog: pytest.LogCaptureFixture
) -> None:
    scenario = SCENARIOS[name]
    _prepare(ask_world)
    world = ask_world
    email = world.admin_rows(
        "SELECT email FROM grounded.users WHERE id = %s", (world.tenant.employee,)
    )[0][0]
    ids = [
        *_forms(world.tenant.tenant),
        *_forms(world.tenant.employee),
        *_forms(world.tenant.admin),
    ]
    secret_texts = [SENTINEL_Q, SENTINEL_C, email]
    answer_texts = [SENTINEL_A, SENTINEL_T]

    with caplog.at_level(logging.DEBUG):
        response = scenario.run(world)

    # 1. logs
    logs = "\n".join(_record_text(record) for record in caplog.records)
    for value in [*secret_texts, *answer_texts, *ids]:
        assert value not in logs, f"{name}: {value!r} reached a log record"
    tenant_ref = tenant_log_ref(world.client.app.state.auth_hmac_key, world.tenant.tenant)  # type: ignore[attr-defined]
    if name not in {"unauthenticated", "invalid_body", "suspended_tenant"}:
        assert tenant_ref in logs, f"{name}: no log line carries the keyed tenant hash"
        assert "request_id=" in logs

    # 2. the response (the last one the scenario returned)
    if response is not None:
        shown = response.text + repr(dict(response.headers))
        # An answered response carries the answer, the title and the quote, and nothing else of the document.
        forbidden = [*secret_texts, *ids] + ([] if scenario.shows_answer else answer_texts)
        for value in forbidden:
            assert value not in shown, f"{name}: {value!r} is in the response"

    # 3. metering rows: every column except their own keys
    with psycopg.connect(world.handle.admin_test_url()) as conn:
        rows = conn.execute(
            "SELECT row_to_json(u)::jsonb - 'id' - 'tenant_id' FROM grounded.answer_usage AS u WHERE tenant_id = %s",
            (world.tenant.tenant,),
        ).fetchall()
    flat = repr(rows)
    for value in [*secret_texts, *answer_texts, *ids]:
        assert value not in flat, f"{name}: {value!r} is in a metering row"
    if scenario.metered:
        assert rows, f"{name}: the request left no metering row"

    # 4. Valkey keys
    client = redis.Redis.from_url(VALKEY_TEST_URL)
    try:
        keys = " ".join(str(key) for key in client.keys("*"))
    finally:
        client.close()
    for value in [*secret_texts, *answer_texts, *ids]:
        assert value not in keys, f"{name}: {value!r} is in a Valkey key"

    # 5. what the provider received: the question and the chunk text, no ids, no title
    for request in world.model.requests:
        seen = repr(request)
        assert SENTINEL_T not in seen and email not in seen
        assert not any(value in seen for value in ids)

"""
POST /ask over HTTP, through the real app (ADR 0033, Step 5c-2): the real lifespan, database as app_user, Valkey, the
rate limiter, the in-flight set, retrieval, the daily cap and the metering writes. Only the embedding model and the
answer model are fakes (tests/db/ask_http_support.py).

Grouped as the plan was: the contract, tenant isolation, the limits and the cap, the provider policy, failures and
their metering rows, the metering-failure flag, and "no connection is held while the model runs".
"""

import math
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from redis.asyncio import Redis

from grounded_answers.answering.types import (
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
)
from grounded_answers.ask.types import Caller, StoreUnavailable, TenantState, UsageRecord
from grounded_answers.ask.valkey import AskLimiter
from grounded_answers.retrieval import RetrievedChunk
from tests.answering_support import answer_json, result_of
from tests.db.ask_http_support import CHUNK_BODY, GOOD_REPLY, QUOTE, AskWorld
from tests.db.auth_support import VALKEY_TEST_URL

pytestmark = pytest.mark.db

QUESTION = "How many days of notice?"
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class FakeClock:
    def __init__(self, start_ms: int = 1_800_000_000_000) -> None:
        self.now = start_ms

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


# ---- the contract ----


def test_an_answer_has_the_documented_shape_and_is_metered(ask_world: AskWorld) -> None:
    response = ask_world.post()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "answered"
    assert UUID_PATTERN.match(body["request_id"])
    assert body["answer"] == "Notice is thirty days. [1]"
    (citation,) = body["citations"]
    assert citation == {
        "ref": 1,
        "document_id": str(ask_world.document_id),
        "title": "Handbook",
        "version": 1,
        "clause": "4.1",
        "clause_kind": "clause",
        "heading": "Notice period",
        "quote": "Notice is thirty days.",
    }
    (row,) = ask_world.usage()
    assert (row["provider"], row["model"], row["outcome"]) == (
        "groq",
        "openai/gpt-oss-120b",
        "answered",
    )
    assert (row["attempts"], row["retrieved_count"]) == (1, 1)
    assert (row["input_tokens"], row["output_tokens"], row["cost_micro_usd"]) == (1000, 100, 210)
    assert row["invalid_codes"] == []


def test_a_quote_before_any_clause_label_cites_the_section(ask_world: AskWorld) -> None:
    _, token = ask_world.new_tenant(body=f"{QUOTE} The rest is not labelled.")
    assert ask_world.post(token=token).json()["citations"][0]["clause"] == "4"
    assert ask_world.post(token=token).json()["citations"][0]["clause_kind"] == "section"


def test_employee_and_admin_may_both_ask(ask_world: AskWorld) -> None:
    assert ask_world.post(token=ask_world.employee_token).status_code == 200
    assert ask_world.post(token=ask_world.admin_token).status_code == 200


def test_the_question_is_trimmed_and_reaches_the_model_with_the_chunk_text_only(
    ask_world: AskWorld,
) -> None:
    ask_world.post("   " + QUESTION + "  \n")
    (request,) = ask_world.model.requests
    assert request.question == QUESTION
    assert [source.text for source in request.sources] == [CHUNK_BODY]


@pytest.mark.parametrize(
    "reason",
    ["not_in_documents", "needs_clarification", "conflicting_sources"],
)
def test_a_refusal_is_a_200_with_our_message(ask_world: AskWorld, reason: str) -> None:
    ask_world.model.script = [result_of(answer_json(refused=True, reason=reason, answer=""))]
    response = ask_world.post()
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "refused" and body["reason"] == reason
    assert "couldn't" in body["message"] or "don't agree" in body["message"]
    assert ask_world.outcomes() == [f"refused_{reason}"]


def test_a_tenant_with_no_documents_is_refused_without_a_model_call(ask_world: AskWorld) -> None:
    other, token = ask_world.new_tenant(with_document=False)
    body = ask_world.post(token=token).json()
    assert body["status"] == "refused" and body["reason"] == "not_in_documents"
    assert ask_world.model.calls == 0
    assert ask_world.outcomes(other.tenant) == ["no_sources"]


def test_two_invalid_answers_are_could_not_verify(ask_world: AskWorld) -> None:
    bad = result_of(
        answer_json(
            answer="Notice is 99 days.", citations=[{"ref": 1, "quote": "Notice is thirty days."}]
        )
    )
    ask_world.model.script = [bad, bad]
    body = ask_world.post().json()
    assert body["status"] == "refused" and body["reason"] == "could_not_verify"
    (row,) = ask_world.usage()
    assert row["outcome"] == "could_not_verify" and row["attempts"] == 2
    assert row["invalid_codes"] == ["number_not_grounded", "number_not_grounded"]


# ---- the request itself ----


def test_no_token_is_401_and_no_model_is_called(ask_world: AskWorld) -> None:
    response = ask_world.client.post("/ask", json={"question": QUESTION})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert ask_world.model.calls == 0 and ask_world.usage() == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": "   \n\t "},
        {"question": "x" * 1001},
        {"question": 123},
        {"question": QUESTION, "tenant_id": str(uuid.uuid4())},
        {"question": QUESTION, "user_id": str(uuid.uuid4())},
        {"question": "bad\x00question"},
        {"question": "bell\x07"},
    ],
)
def test_a_bad_body_is_422_and_never_echoed(ask_world: AskWorld, body: dict[str, Any]) -> None:
    response = ask_world.client.post(
        "/ask", json=body, headers={"Authorization": f"Bearer {ask_world.employee_token}"}
    )
    assert response.status_code == 422
    text = response.text
    for value in body.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in text
    assert ask_world.model.calls == 0 and ask_world.usage() == []


def test_a_question_of_exactly_one_thousand_characters_is_accepted(ask_world: AskWorld) -> None:
    assert ask_world.post("q" * 1000).status_code == 200


def test_unauthenticated_with_a_bad_body_is_still_401(ask_world: AskWorld) -> None:
    response = ask_world.client.post("/ask", json={"question": ""})
    assert response.status_code == 401


def test_a_suspended_tenant_is_refused_before_anything_else(ask_world: AskWorld) -> None:
    with psycopg.connect(ask_world.handle.admin_test_url()) as conn:
        conn.execute(
            "UPDATE grounded.tenants SET status = 'suspended' WHERE id = %s",
            (ask_world.tenant.tenant,),
        )
    response = ask_world.post()
    assert response.status_code == 403
    assert ask_world.model.calls == 0 and ask_world.usage() == []


def test_the_route_does_not_exist_when_ask_is_off(
    accounts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from grounded_answers.config import get_settings
    from grounded_answers.main import create_app
    from tests.db.auth_support import SELECTOR

    monkeypatch.delenv("ASK_ENABLED", raising=False)
    get_settings.cache_clear()
    with TestClient(
        create_app(), raise_server_exceptions=False, backend_options=SELECTOR
    ) as client:
        response = client.post("/ask", json={"question": QUESTION})
        assert response.status_code == 404
    get_settings.cache_clear()


# ---- tenant isolation ----


def test_another_tenants_text_never_reaches_the_model_or_the_response(ask_world: AskWorld) -> None:
    _, other_token = ask_world.new_tenant(
        body="SENTINEL-OTHER-TENANT 4.1 Notice is thirty days. Other tenant text."
    )
    ask_world.post()
    (request,) = ask_world.model.requests
    assert "SENTINEL-OTHER-TENANT" not in repr(request)
    assert ask_world.post().status_code == 200
    ask_world.model.requests.clear()
    ask_world.post(token=other_token)
    (other_request,) = ask_world.model.requests
    assert "SENTINEL-OTHER-TENANT" in repr(other_request)
    assert CHUNK_BODY not in repr(other_request)


def test_a_tenants_cap_ignores_another_tenants_spending(ask_world: AskWorld) -> None:
    other, _ = ask_world.new_tenant()
    ask_world.add_usage(10_000_000, tenant=other.tenant)  # the other tenant is far over its cap
    assert ask_world.post().status_code == 200


# ---- rate limits ----


def test_the_sixth_question_in_a_minute_is_429_with_retry_after(ask_world: AskWorld) -> None:
    clock = FakeClock()
    cache = ask_world.client.app.state.cache  # type: ignore[attr-defined]
    key = ask_world.client.app.state.rate_limit_hmac_key  # type: ignore[attr-defined]
    ask_world.rebuild(limiter=AskLimiter(cache, key, clock, tenant_limit=20, user_limit=5))
    for _ in range(5):
        assert ask_world.post().status_code == 200
    calls = ask_world.model.calls
    response = ask_world.post()
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert response.headers["cache-control"] == "no-store"
    assert ask_world.model.calls == calls  # refused before any work
    assert len(ask_world.usage()) == 5  # a rate-limited request is not metered
    clock.advance(60_000)
    assert ask_world.post().status_code == 200


def test_the_tenant_limit_is_shared_by_its_users(ask_world: AskWorld) -> None:
    cache = ask_world.client.app.state.cache  # type: ignore[attr-defined]
    key = ask_world.client.app.state.rate_limit_hmac_key  # type: ignore[attr-defined]
    ask_world.rebuild(limiter=AskLimiter(cache, key, FakeClock(), tenant_limit=3, user_limit=5))
    assert ask_world.post(token=ask_world.employee_token).status_code == 200
    assert ask_world.post(token=ask_world.admin_token).status_code == 200
    assert ask_world.post(token=ask_world.employee_token).status_code == 200
    assert ask_world.post(token=ask_world.admin_token).status_code == 429


def test_valkey_down_fails_closed_without_a_model_call(ask_world: AskWorld) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_timeout=0.5, socket_connect_timeout=0.5)
    key = ask_world.client.app.state.rate_limit_hmac_key  # type: ignore[attr-defined]
    ask_world.rebuild(limiter=AskLimiter(dead, key, FakeClock(), tenant_limit=20, user_limit=5))
    response = ask_world.post()
    assert response.status_code == 503 and response.json()["code"] == "service_unavailable"
    assert ask_world.model.calls == 0 and ask_world.usage() == []


# ---- in-flight limit ----


def test_a_third_concurrent_question_of_a_tenant_is_busy_and_slots_come_back(
    ask_world: AskWorld,
) -> None:
    ask_world.model.delay = 1.5
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(ask_world.post)
        second = pool.submit(ask_world.post)
        deadline = time.monotonic() + 10
        while ask_world.model.calls < 2:
            assert time.monotonic() < deadline, "the first two requests never reached the model"
            time.sleep(0.02)
        third = ask_world.post()
        assert third.status_code == 429 and third.json()["code"] == "busy"
        assert int(third.headers["retry-after"]) >= 1
        assert first.result().status_code == 200 and second.result().status_code == 200
    ask_world.model.delay = 0
    assert ask_world.post().status_code == 200  # the slots were released


def test_another_tenants_questions_do_not_use_this_tenants_slots(ask_world: AskWorld) -> None:
    _, other_token = ask_world.new_tenant()
    ask_world.model.delay = 1.0
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(ask_world.post) for _ in range(2)]
        deadline = time.monotonic() + 10
        while ask_world.model.calls < 2:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        other = ask_world.post(token=other_token)
        assert other.status_code == 200
        assert all(f.result().status_code == 200 for f in futures)


def test_a_failed_request_frees_its_slot(ask_world: AskWorld) -> None:
    ask_world.model.script = [ProviderRejected("bad key", 401)]
    for _ in range(
        4
    ):  # more failures than the limit of 2 in flight: a leak would turn these into 429
        assert ask_world.post().status_code == 503


# ---- the daily cap ----


def _seconds_to_midnight() -> int:
    now = datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return math.ceil((midnight - now).total_seconds())


def test_spend_at_the_cap_is_429_with_the_time_to_midnight_and_no_model_call(
    ask_world: AskWorld,
) -> None:
    ask_world.set_settings(cap=1000)
    ask_world.add_usage(1000)
    response = ask_world.post()
    assert response.status_code == 429
    assert response.json()["code"] == "daily_limit"
    assert abs(int(response.headers["retry-after"]) - _seconds_to_midnight()) <= 5
    assert ask_world.model.calls == 0
    assert (
        len(ask_world.usage()) == 1
    )  # only the row the test added: a refused request is not metered


def test_spend_just_under_the_cap_is_served_and_the_next_question_is_not(
    ask_world: AskWorld,
) -> None:
    ask_world.set_settings(cap=300)
    ask_world.add_usage(299)
    assert ask_world.post().status_code == 200  # 299 < 300: served, and it costs 210
    over = ask_world.post()
    assert over.status_code == 429 and over.json()["code"] == "daily_limit"
    assert ask_world.model.calls == 1


def test_yesterdays_spend_does_not_count(ask_world: AskWorld) -> None:
    ask_world.set_settings(cap=1000)
    ask_world.add_usage(5000, days_ago=1)
    ask_world.add_usage(5000, days_ago=2)
    assert ask_world.post().status_code == 200


def test_a_cap_of_zero_refuses_everything(ask_world: AskWorld) -> None:
    ask_world.set_settings(cap=0)
    assert ask_world.post().status_code == 429
    assert ask_world.model.calls == 0


def test_a_database_failure_reading_the_state_fails_closed(ask_world: AskWorld) -> None:
    class Broken:
        async def read_state(self, caller: Caller) -> TenantState:
            raise StoreUnavailable("read_state")

        async def retrieve(self, *args: Any) -> list[RetrievedChunk]:
            raise AssertionError("not reached")

        async def record(self, *args: Any) -> None:
            raise AssertionError("not reached")

    ask_world.rebuild(store=Broken())
    response = ask_world.post()
    assert response.status_code == 503 and response.json()["code"] == "service_unavailable"
    assert ask_world.model.calls == 0 and ask_world.usage() == []


# ---- settings and provider policy ----


@pytest.mark.parametrize("remove_row", [True, False])
def test_a_tenant_not_configured_is_503_not_configured(
    ask_world: AskWorld, remove_row: bool
) -> None:
    if remove_row:
        with psycopg.connect(ask_world.handle.admin_test_url()) as conn:
            conn.execute(
                "DELETE FROM grounded.tenant_settings WHERE tenant_id = %s",
                (ask_world.tenant.tenant,),
            )
    else:
        ask_world.set_settings(providers=())
    response = ask_world.post()
    assert response.status_code == 503 and response.json()["code"] == "not_configured"
    assert "Retry-After" not in response.headers and "retry-after" not in response.headers
    assert ask_world.model.calls == 0
    assert ask_world.outcomes() == ["tenant_not_configured"]


def test_a_free_tier_provider_for_a_real_tenant_is_never_called(ask_world: AskWorld) -> None:
    with psycopg.connect(ask_world.handle.admin_test_url()) as conn:
        # Written directly: the admin CLI would refuse this combination (ADR 0033, decision 5), and the web app must too.
        conn.execute(
            "UPDATE grounded.tenant_settings SET data_class = 'real' WHERE tenant_id = %s",
            (ask_world.tenant.tenant,),
        )
    response = ask_world.post()
    assert response.status_code == 503 and response.json()["code"] == "service_unavailable"
    assert ask_world.model.calls == 0
    (row,) = ask_world.usage()
    assert (row["outcome"], row["provider"]) == ("provider_disallowed", "groq")


def test_a_provider_without_a_configured_key_is_a_metered_service_error(
    ask_world: AskWorld,
) -> None:
    ask_world.set_settings(providers=("gemini",))  # no key and no adapter for gemini in this app
    response = ask_world.post()
    assert response.status_code == 503
    assert ask_world.model.calls == 0
    assert ask_world.outcomes() == ["service_error"]


# ---- failures: 503, a metering row, and a slot that comes back ----


@pytest.mark.parametrize(
    ("error", "outcome", "retry_after"),
    [
        (ProviderUnavailable("down"), "provider_error", 5),
        (ProviderTimeout("slow"), "timeout", 5),
        (ProviderRateLimited("slow down", 12.0), "provider_rate_limited", 12),
    ],
)
def test_a_provider_failure_is_503_try_again_and_metered(
    ask_world: AskWorld, error: Exception, outcome: str, retry_after: int
) -> None:
    ask_world.model.script = [error]
    response = ask_world.post()
    assert response.status_code == 503
    assert response.json()["code"] == "try_again"
    assert int(response.headers["retry-after"]) == retry_after
    assert ask_world.outcomes() == [outcome]


def test_tokens_of_a_first_attempt_are_counted_when_the_second_fails(ask_world: AskWorld) -> None:
    bad = result_of(
        answer_json(
            answer="Notice is 99 days.", citations=[{"ref": 1, "quote": "Notice is thirty days."}]
        )
    )
    ask_world.model.script = [bad, ProviderUnavailable("down")]
    assert ask_world.post().status_code == 503
    (row,) = ask_world.usage()
    assert row["outcome"] == "provider_error"
    assert (row["input_tokens"], row["output_tokens"], row["cost_micro_usd"]) == (1000, 100, 210)


def test_an_embedder_that_is_not_ready_is_503(ask_world: AskWorld) -> None:
    from grounded_answers.embedding.query import QueryEmbedderNotReady

    class Loading:
        async def embed(self, text: str) -> list[float]:
            raise QueryEmbedderNotReady("loading")

    ask_world.rebuild(embedder=Loading())
    response = ask_world.post()
    assert response.status_code == 503
    assert int(response.headers["retry-after"]) == 10
    assert ask_world.outcomes() == ["service_error"] and ask_world.model.calls == 0


def test_a_retrieval_failure_is_503_and_metered(ask_world: AskWorld) -> None:
    inner = ask_world.deps.store

    class NoRetrieval:
        async def read_state(self, caller: Caller) -> TenantState:
            return await inner.read_state(caller)  # type: ignore[no-any-return]

        async def retrieve(self, *args: Any) -> list[RetrievedChunk]:
            raise StoreUnavailable("retrieve")

        async def record(self, caller: Caller, record: UsageRecord) -> None:
            await inner.record(caller, record)

    ask_world.rebuild(store=NoRetrieval())
    assert ask_world.post().status_code == 503
    assert ask_world.outcomes() == ["service_error"] and ask_world.model.calls == 0


def test_an_unexpected_error_is_500_with_no_detail_and_metered(ask_world: AskWorld) -> None:
    inner = ask_world.deps.store

    class Explodes:
        async def read_state(self, caller: Caller) -> TenantState:
            return await inner.read_state(caller)  # type: ignore[no-any-return]

        async def retrieve(self, *args: Any) -> list[RetrievedChunk]:
            raise RuntimeError("SENTINEL-internal-detail")

        async def record(self, caller: Caller, record: UsageRecord) -> None:
            await inner.record(caller, record)

    ask_world.rebuild(store=Explodes())
    response = ask_world.post()
    assert response.status_code == 500
    assert "SENTINEL" not in response.text
    assert set(response.json()) == {"detail", "request_id"}
    assert ask_world.outcomes() == ["service_error"]
    assert ask_world.post().status_code == 500  # and the slot was freed


def test_the_deadline_ends_a_stuck_model_as_a_timeout(
    ask_world: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    ask_world.deps.config = type(ask_world.deps.config)(deadline_seconds=5.0, top_k=5)
    ask_world.rebuild()
    ask_world.model.delay = 30
    started = time.monotonic()
    response = ask_world.post()
    elapsed = time.monotonic() - started
    assert response.status_code == 503 and response.json()["code"] == "try_again"
    assert 4.5 <= elapsed < 9
    assert ask_world.outcomes() == ["timeout"]


# ---- the metering-failure flag ----


def test_a_persistent_metering_failure_blocks_that_tenant_and_only_that_tenant(
    ask_world: AskWorld,
) -> None:
    other, other_token = ask_world.new_tenant()
    app_role = "app_user"
    with psycopg.connect(ask_world.handle.admin_test_url()) as conn:
        conn.execute(f"REVOKE INSERT ON grounded.answer_usage FROM {app_role}")
    try:
        first = ask_world.post()
        assert first.status_code == 200  # the user still gets the answer that was paid for
        assert ask_world.usage() == []  # but nothing was recorded
        blocked = ask_world.post()
        assert blocked.status_code == 503 and blocked.json()["code"] == "service_unavailable"
        assert ask_world.model.calls == 1  # the second question never reached the model
    finally:
        with psycopg.connect(ask_world.handle.admin_test_url()) as conn:
            conn.execute(f"GRANT INSERT ON grounded.answer_usage TO {app_role}")
    # The flag belongs to the tenant that failed. The other tenant, with the grant back, is served and metered.
    assert ask_world.post(token=other_token).status_code == 200
    assert ask_world.outcomes(other.tenant) == ["answered"]
    # The blocked tenant stays blocked until the flag expires (5 minutes); flushing Valkey stands in for that.
    assert ask_world.post().status_code == 503
    from tests.db.auth_support import flush_test_valkey

    flush_test_valkey()
    assert ask_world.post().status_code == 200
    assert ask_world.outcomes() == ["answered"]


# ---- no connection is held while the model runs ----


def test_no_transaction_is_open_while_the_model_is_called(ask_world: AskWorld) -> None:
    ask_world.model.delay = 1.5
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ask_world.post)
        assert ask_world.model.started.wait(timeout=10)
        rows = ask_world.admin_rows(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_user' "
            "AND state IN ('idle in transaction', 'idle in transaction (aborted)')"
        )
        assert rows == [(0,)], "a transaction of app_user stayed open during the model call"
        assert future.result().status_code == 200


def test_the_valkey_keys_hold_no_raw_ids(ask_world: AskWorld) -> None:
    ask_world.post()
    import redis

    client = redis.Redis.from_url(VALKEY_TEST_URL)
    try:
        keys = [str(key) for key in client.keys("grounded:ask:*")]
    finally:
        client.close()
    assert keys, "the request left no keys at all: the limiter did not run"
    for key in keys:
        for value in (ask_world.tenant.tenant, ask_world.tenant.employee, ask_world.tenant.admin):
            assert str(value) not in key and value.hex not in key


def test_the_prompt_text_is_the_only_content_the_model_gets(ask_world: AskWorld) -> None:
    ask_world.post()
    (request,) = ask_world.model.requests
    text = repr(request)
    for value in (ask_world.tenant.tenant, ask_world.tenant.employee, ask_world.document_id):
        assert str(value) not in text and value.hex not in text
    assert (
        "Handbook" not in text and "Notice period" not in text
    )  # title and heading stay on our side
    assert GOOD_REPLY not in text


# ---- the running app is wired with the settings ----


def test_the_running_service_uses_the_configured_limits(ask_world: AskWorld) -> None:
    deps = ask_world.deps
    assert (deps.limiter.user_limit, deps.limiter.tenant_limit) == (5, 20)
    assert (deps.in_flight.limit, deps.in_flight.hold_ms) == (2, 17_000)
    assert (deps.config.deadline_seconds, deps.config.top_k) == (12.0, 5)
    assert deps.log_key == ask_world.client.app.state.auth_hmac_key  # type: ignore[attr-defined]
    assert deps.flag.ttl_seconds == 300


def test_the_limits_come_from_the_environment(
    ask_world: AskWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A second app, started with other settings, is built with them."""
    from fastapi.testclient import TestClient

    from grounded_answers.config import get_settings
    from grounded_answers.main import create_app
    from tests.db.auth_support import SELECTOR

    monkeypatch.setenv("ASK_RATE_USER_PER_MIN", "7")
    monkeypatch.setenv("ASK_RATE_TENANT_PER_MIN", "11")
    monkeypatch.setenv("ASK_INFLIGHT_PER_TENANT", "3")
    monkeypatch.setenv("ASK_DEADLINE_SECONDS", "9")
    monkeypatch.setenv("RETRIEVAL_TOP_K", "8")
    get_settings.cache_clear()
    with TestClient(
        create_app(), raise_server_exceptions=False, backend_options=SELECTOR
    ) as client:
        deps = client.app.state.ask_deps  # type: ignore[attr-defined]
        assert (deps.limiter.user_limit, deps.limiter.tenant_limit) == (7, 11)
        assert (deps.in_flight.limit, deps.in_flight.hold_ms) == (3, 14_000)
        assert (deps.config.deadline_seconds, deps.config.top_k) == (9.0, 8)

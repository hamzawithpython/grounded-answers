"""
The flow of POST /ask with fake interfaces (ADR 0033, Step 5c-1): the order of the stages, what is refused before any
work, the mapping of every outcome to a result and a metering row, the cap, the provider policy, the deadline,
cancellation, and the metering-failure flag. No database, no Valkey, no network.
"""

import asyncio
import logging
from datetime import UTC, datetime

import pytest

from grounded_answers.answering.pricing import cost_micro_usd
from grounded_answers.answering.types import (
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    Usage,
)
from grounded_answers.ask.service import AskConfig, seconds_until_utc_midnight
from grounded_answers.ask.types import (
    Answered,
    Limited,
    Refused,
    StoreUnavailable,
    TenantAnswerSettings,
    TenantState,
    Unavailable,
)
from grounded_answers.embedding.query import (
    QueryEmbedderBusy,
    QueryEmbedderNotReady,
    QueryEmbedderTimeout,
)
from tests.answering_support import answer_json, result_of
from tests.ask_support import CAPTION, GOOD_REPLY, SlowModel, build

pytestmark = pytest.mark.anyio

QUESTION = "How many days of notice?"
FULL_ORDER = [
    "limiter",
    "flag.read",
    "inflight.acquire",
    "store.read_state",
    "embed",
    "store.retrieve",
    "model",
    "store.record",
    "inflight.release",
]
INVALID_REPLY = answer_json(answer="Notice is 99 days.", citations=[{"ref": 1, "quote": CAPTION}])


def _state(
    spent: int = 0,
    cap: int = 50_000,
    providers: tuple[str, ...] = ("groq",),
    data_class: str = "fictional",
) -> TenantState:
    return TenantState(TenantAnswerSettings(providers, data_class, cap), spent)


# ---- the happy path and the order of the stages ----


async def test_answered_runs_every_stage_in_order_and_meters_it() -> None:
    rig = build()
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Answered)
    assert result.answer.text == "Notice is thirty days."
    assert rig.trace == FULL_ORDER
    (record,) = rig.store.records
    assert (record.provider, record.model, record.outcome) == (
        "groq",
        "openai/gpt-oss-120b",
        "answered",
    )
    assert (record.attempts, record.retrieved_count) == (1, 1)
    assert (record.input_tokens, record.output_tokens) == (1000, 100)
    assert record.cost_micro_usd == cost_micro_usd("openai/gpt-oss-120b", Usage(1000, 100, None))
    assert record.invalid_codes == ()
    assert rig.in_flight.held == set()


async def test_the_prompt_gets_the_question_and_the_chunk_text_only() -> None:
    rig = build()
    await rig.service.ask(rig.caller, QUESTION, "req-1")
    (request,) = rig.model.requests  # type: ignore[attr-defined]
    assert request.question == QUESTION
    assert [source.text for source in request.sources] == [CAPTION]
    assert rig.embedder.texts == [QUESTION]


# ---- refusals are results, not errors ----


@pytest.mark.parametrize(
    ("reason", "outcome"),
    [
        ("not_in_documents", "refused_not_in_documents"),
        ("needs_clarification", "refused_needs_clarification"),
        ("conflicting_sources", "refused_conflicting_sources"),
    ],
)
async def test_a_model_refusal_is_a_refused_result_with_our_message(
    reason: str, outcome: str
) -> None:
    rig = build([result_of(answer_json(refused=True, reason=reason, answer=""))])
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Refused)
    assert result.refusal.kind == reason
    assert [r.outcome for r in rig.store.records] == [outcome]


async def test_no_chunks_is_a_refusal_without_calling_the_model() -> None:
    rig = build()
    rig.store.chunks = []
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Refused) and result.refusal.kind == "not_in_documents"
    assert "model" not in rig.trace
    (record,) = rig.store.records
    assert (record.outcome, record.retrieved_count, record.attempts) == ("no_sources", 0, 0)


async def test_two_invalid_answers_end_in_could_not_verify_with_the_codes_metered() -> None:
    rig = build([result_of(INVALID_REPLY), result_of(INVALID_REPLY)])
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Refused) and result.refusal.kind == "could_not_verify"
    (record,) = rig.store.records
    assert record.outcome == "could_not_verify"
    assert record.attempts == 2
    assert record.invalid_codes == ("number_not_grounded", "number_not_grounded")
    assert (record.input_tokens, record.output_tokens) == (2000, 200)


# ---- limits: refused before any work, nothing metered ----


async def test_a_rate_limited_request_stops_at_the_first_stage() -> None:
    rig = build()
    rig.limiter.wait = 37
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert result == Limited("req-1", "rate_limited", 37)
    assert rig.trace == ["limiter"]
    assert rig.store.records == []


async def test_a_limiter_failure_fails_closed_without_any_work() -> None:
    rig = build()
    rig.limiter.down = True
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Unavailable) and result.code == "service_unavailable"
    assert rig.trace == ["limiter"]


async def test_the_metering_flag_refuses_and_a_flag_read_failure_fails_closed() -> None:
    rig = build()
    rig.flag.value = True
    first = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(first, Unavailable) and first.code == "service_unavailable"
    assert rig.trace == ["limiter", "flag.read"]

    other = build()
    other.flag.read_down = True
    second = await other.service.ask(other.caller, QUESTION, "req-2")
    assert isinstance(second, Unavailable)
    assert other.trace == ["limiter", "flag.read"]


async def test_a_full_in_flight_set_is_busy_and_a_failure_fails_closed() -> None:
    rig = build()
    rig.in_flight.grant = False
    busy = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(busy, Limited) and busy.code == "busy"
    assert rig.trace == [
        "limiter",
        "flag.read",
        "inflight.acquire",
    ]  # no release: nothing was taken

    down = build()
    down.in_flight.down = True
    result = await down.service.ask(down.caller, QUESTION, "req-2")
    assert isinstance(result, Unavailable)
    assert "store.read_state" not in down.trace


# ---- the daily cap ----


@pytest.mark.parametrize(("spent", "cap"), [(50_000, 50_000), (50_001, 50_000), (0, 0)])
async def test_spend_at_or_over_the_cap_is_refused_before_embedding_or_the_model(
    spent: int, cap: int
) -> None:
    rig = build()
    rig.store.state = _state(spent=spent, cap=cap)
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert result == Limited("req-1", "daily_limit", 3600)  # one hour to 00:00 UTC
    assert rig.trace == [
        "limiter",
        "flag.read",
        "inflight.acquire",
        "store.read_state",
        "inflight.release",
    ]
    assert rig.store.records == []


async def test_spend_just_under_the_cap_is_served() -> None:
    rig = build()
    rig.store.state = _state(spent=49_999, cap=50_000)
    assert isinstance(await rig.service.ask(rig.caller, QUESTION, "req-1"), Answered)


async def test_a_state_read_failure_fails_closed_and_frees_the_slot() -> None:
    for error in (StoreUnavailable(), None):
        rig = build()
        if error is None:
            rig.store.read_delay = 1.0
            rig = build(config=AskConfig(state_timeout=0.05, metering_timeout=0.3))
            rig.store.read_delay = 1.0
        else:
            rig.store.read_error = error
        result = await rig.service.ask(rig.caller, QUESTION, "req-1")
        assert isinstance(result, Unavailable) and result.code == "service_unavailable"
        assert "embed" not in rig.trace and "model" not in rig.trace
        assert rig.store.records == []
        assert rig.in_flight.held == set()


@pytest.mark.parametrize("state", [TenantState(None, 0), _state(providers=())])
async def test_a_tenant_without_settings_or_providers_is_not_configured_and_metered(
    state: TenantState,
) -> None:
    rig = build()
    rig.store.state = state
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Unavailable) and result.code == "not_configured"
    assert "model" not in rig.trace and "embed" not in rig.trace
    assert [r.outcome for r in rig.store.records] == ["tenant_not_configured"]


def test_seconds_until_utc_midnight() -> None:
    assert seconds_until_utc_midnight(datetime(2026, 10, 26, 23, 0, tzinfo=UTC)) == 3600
    assert seconds_until_utc_midnight(datetime(2026, 10, 26, 0, 0, tzinfo=UTC)) == 86400
    assert seconds_until_utc_midnight(datetime(2026, 10, 26, 23, 59, 59, 500000, tzinfo=UTC)) == 1


# ---- provider policy ----


async def test_a_free_tier_provider_for_a_real_tenant_is_not_called() -> None:
    rig = build()
    rig.store.state = _state(providers=("groq",), data_class="real")
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Unavailable) and result.code == "service_unavailable"
    assert "model" not in rig.trace and "embed" not in rig.trace
    (record,) = rig.store.records
    assert (record.outcome, record.provider) == ("provider_disallowed", "groq")


async def test_a_missing_key_is_a_metered_service_error_without_a_call() -> None:
    rig = build()
    rig.providers.models.clear()
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Unavailable) and result.code == "service_unavailable"
    assert [r.outcome for r in rig.store.records] == ["service_error"]
    assert "embed" not in rig.trace


async def test_a_model_without_a_list_price_is_not_called() -> None:
    rig = build()
    rig.providers.models["groq"].model = "unpriced-model"  # type: ignore[attr-defined]
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Unavailable)
    assert [r.outcome for r in rig.store.records] == ["service_error"]
    assert "model" not in rig.trace


async def test_only_the_first_provider_on_the_list_is_used() -> None:
    rig = build()
    rig.store.state = _state(
        providers=("gemini", "groq")
    )  # gemini has no adapter: no fallback in v1
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Unavailable)
    assert "model" not in rig.trace
    assert rig.store.records[0].provider == "gemini"


# ---- embedding and retrieval failures ----


@pytest.mark.parametrize(
    ("error", "code", "retry", "outcome"),
    [
        (QueryEmbedderNotReady("loading"), "service_unavailable", 10, "service_error"),
        (QueryEmbedderBusy(), "try_again", 2, "service_error"),
        (QueryEmbedderTimeout(), "try_again", 5, "timeout"),
        (TimeoutError(), "try_again", 5, "timeout"),
    ],
)
async def test_embedding_failures(error: Exception, code: str, retry: int, outcome: str) -> None:
    rig = build()
    rig.embedder.error = error
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert result == Unavailable("req-1", code, retry)
    assert [r.outcome for r in rig.store.records] == [outcome]
    assert "store.retrieve" not in rig.trace and "model" not in rig.trace
    assert rig.in_flight.held == set()


async def test_a_slow_embedding_runs_into_the_stage_timeout() -> None:
    rig = build(config=AskConfig(embed_timeout=0.05, metering_timeout=0.3))
    rig.embedder.delay = 1.0
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Unavailable) and result.code == "try_again"
    assert [r.outcome for r in rig.store.records] == ["timeout"]


async def test_retrieval_failures() -> None:
    rig = build()
    rig.store.retrieve_error = StoreUnavailable()
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Unavailable) and result.code == "service_unavailable"
    assert [r.outcome for r in rig.store.records] == ["service_error"]
    assert "model" not in rig.trace


# ---- provider failures are 503s, with the tokens already used counted ----


@pytest.mark.parametrize(
    ("error", "outcome", "retry"),
    [
        (ProviderUnavailable("down"), "provider_error", 5),
        (ProviderRejected("bad key", 401), "provider_error", 5),
        (ProviderTimeout("slow"), "timeout", 5),
        (ProviderRateLimited("slow down", 12.0), "provider_rate_limited", 12),
        (ProviderRateLimited("daily quota", 4000.0), "provider_rate_limited", 30),
        (ProviderRateLimited("no hint", None), "provider_rate_limited", 5),
    ],
)
async def test_a_provider_failure_is_try_again_and_metered(
    error: Exception, outcome: str, retry: int
) -> None:
    rig = build([error] * 4)
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert result == Unavailable("req-1", "try_again", retry)
    (record,) = rig.store.records
    assert record.outcome == outcome
    assert (record.input_tokens, record.output_tokens, record.cost_micro_usd) == (0, 0, 0)
    assert rig.in_flight.held == set()


async def test_tokens_of_an_earlier_attempt_are_counted_when_the_second_attempt_fails() -> None:
    rig = build(
        [result_of(INVALID_REPLY), ProviderUnavailable("down"), ProviderUnavailable("down")]
    )
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Unavailable)
    (record,) = rig.store.records
    assert record.outcome == "provider_error"
    assert (record.input_tokens, record.output_tokens) == (1000, 100)
    assert record.cost_micro_usd == cost_micro_usd("openai/gpt-oss-120b", Usage(1000, 100, None))
    assert record.attempts == 2


async def test_an_unexpected_exception_is_metered_once_and_raised() -> None:
    rig = build()
    rig.store.retrieve_error = RuntimeError("bug")
    with pytest.raises(RuntimeError, match="bug"):
        await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert [r.outcome for r in rig.store.records] == ["service_error"]
    assert rig.in_flight.held == set()


# ---- the time budget ----


async def test_the_deadline_ends_a_slow_model_call_as_a_metered_timeout() -> None:
    trace: list[str] = []
    rig = build(
        model=SlowModel(5.0, trace), config=AskConfig(deadline_seconds=0.4, metering_timeout=0.3)
    )
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Unavailable) and result.code == "try_again"
    (record,) = rig.store.records
    assert record.outcome == "timeout"
    assert record.latency_ms < 2000
    assert rig.in_flight.held == set()


async def test_the_corrective_retry_is_skipped_when_under_four_seconds_remain() -> None:
    rig = build(
        [result_of(INVALID_REPLY), result_of(GOOD_REPLY)],
        config=AskConfig(deadline_seconds=3.0, metering_timeout=0.3),
    )
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Refused) and result.refusal.kind == "could_not_verify"
    assert rig.model.calls == 1  # type: ignore[attr-defined]
    (record,) = rig.store.records
    assert (record.outcome, record.attempts) == ("could_not_verify", 1)


async def test_the_corrective_retry_runs_with_enough_time() -> None:
    rig = build([result_of(INVALID_REPLY), result_of(GOOD_REPLY)])
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Answered)
    assert rig.model.calls == 2  # type: ignore[attr-defined]
    assert rig.store.records[0].attempts == 2


async def test_each_model_attempt_is_limited_to_eight_seconds_or_the_time_left() -> None:
    rig = build()
    await rig.service.ask(rig.caller, QUESTION, "req-1")
    (request,) = rig.model.requests  # type: ignore[attr-defined]
    assert 0 < request.timeout_seconds <= 8.0


async def test_no_store_method_runs_while_the_model_is_called() -> None:
    rig = build()  # Traced.answer asserts rig.store.active == 0 on every model call
    assert isinstance(await rig.service.ask(rig.caller, QUESTION, "req-1"), Answered)


# ---- the slot is always released ----


async def test_the_slot_is_held_during_the_model_call_and_freed_after() -> None:
    trace: list[str] = []
    model = SlowModel(0.2, trace)
    rig = build(model=model)
    task = asyncio.create_task(rig.service.ask(rig.caller, QUESTION, "req-1"))
    await asyncio.wait_for(model.started.wait(), 5)
    assert rig.in_flight.held == {"req-1"}
    await task
    assert rig.in_flight.held == set()


async def test_a_cancelled_request_frees_its_slot_and_is_still_metered() -> None:
    trace: list[str] = []
    model = SlowModel(30.0, trace)
    rig = build(model=model)
    task = asyncio.create_task(rig.service.ask(rig.caller, QUESTION, "req-1"))
    await asyncio.wait_for(model.started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.in_flight.held == set()
    assert [r.outcome for r in rig.store.records] == ["service_error"]


async def test_a_failed_release_does_not_change_the_result() -> None:
    rig = build()
    rig.in_flight.release_down = True
    assert isinstance(await rig.service.ask(rig.caller, QUESTION, "req-1"), Answered)


# ---- metering failures set the flag ----


async def test_one_failed_metering_insert_is_retried_and_sets_no_flag() -> None:
    rig = build()
    rig.store.record_failures = 1
    assert isinstance(await rig.service.ask(rig.caller, QUESTION, "req-1"), Answered)
    assert rig.store.record_attempts == 2
    assert len(rig.store.records) == 1
    assert rig.flag.set_calls == 0


async def test_two_failed_metering_inserts_set_the_flag_and_still_return_the_answer() -> None:
    rig = build()
    rig.store.record_failures = 2
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Answered)
    assert rig.store.record_attempts == 2
    assert rig.flag.set_calls == 1 and rig.flag.value is True
    # the next request for this tenant is refused
    again = await rig.service.ask(rig.caller, QUESTION, "req-2")
    assert isinstance(again, Unavailable) and again.code == "service_unavailable"
    assert rig.store.record_attempts == 2


async def test_a_metering_insert_that_hangs_counts_as_failed() -> None:
    rig = build(config=AskConfig(metering_timeout=0.05))
    rig.store.record_delay = 5.0
    result = await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert isinstance(result, Answered)
    assert rig.flag.set_calls == 1


async def test_a_flag_that_cannot_be_set_does_not_break_the_answer() -> None:
    rig = build()
    rig.store.record_failures = 2
    rig.flag.set_down = True
    assert isinstance(await rig.service.ask(rig.caller, QUESTION, "req-1"), Answered)


async def test_the_flag_applies_per_tenant() -> None:
    rig = build()
    rig.store.record_failures = 2
    await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert (
        rig.flag.value is True
    )  # the fake holds one value: the real flag is keyed by tenant (db tests)


# ---- logs ----


async def test_the_done_line_has_codes_counts_and_a_tenant_hash_and_no_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = build()
    with caplog.at_level(logging.DEBUG):
        await rig.service.ask(rig.caller, QUESTION, "req-77")
    done = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ask.done")]
    assert len(done) == 1
    line = done[0]
    assert "request_id=req-77" in line and "outcome=answered" in line and "attempts=1" in line
    assert "tenant=" in line
    for raw in (str(rig.caller.tenant_id), str(rig.caller.user_id)):
        assert raw not in caplog.text
        assert raw.replace("-", "") not in caplog.text


# ---- cost in whole micro-dollars, rounded up ----


def test_cost_rounds_up_so_the_cap_never_counts_less_than_the_list_price() -> None:
    model = "openai/gpt-oss-120b"  # $0.15 in, $0.60 out per million tokens: micro-dollars per token
    assert cost_micro_usd(model, Usage(0, 0, None)) == 0
    assert cost_micro_usd(model, Usage(1, 0, None)) == 1  # 0.15 rounds up
    assert cost_micro_usd(model, Usage(0, 1, None)) == 1  # 0.60 rounds up
    assert cost_micro_usd(model, Usage(1000, 100, None)) == 210  # 150 + 60, exact
    assert cost_micro_usd(model, Usage(7, 3, None)) == 3  # 1.05 + 1.80 = 2.85 rounds up to 3

"""
The sentinel test, service level (ADR 0033, decision 8): known strings and ids are put into a request, and every path
of the flow is run. Nothing the user typed, nothing from the documents, nothing the model said, and no raw tenant or
user id may appear in any log record, in any metering record, or in the request the provider receives beyond the
question and the chunk text. Step 5c-2 repeats the check through HTTP, the real database and Valkey.

The test also requires the log lines that should exist (the request id, the keyed hash of the tenant) so it cannot pass
by logging nothing.
"""

import logging
import uuid
from collections.abc import Callable

import pytest

from grounded_answers.answering.types import (
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from grounded_answers.ask.service import AskConfig
from grounded_answers.ask.types import (
    Answered,
    StoreUnavailable,
    TenantAnswerSettings,
    TenantState,
)
from grounded_answers.embedding.query import QueryEmbedderBusy, QueryEmbedderNotReady
from grounded_answers.log_ref import tenant_log_ref
from tests.answering_support import answer_json, make_chunk, result_of
from tests.ask_support import LOG_KEY, Rig, SlowModel, build

pytestmark = pytest.mark.anyio

SENTINEL_Q = "SENTINEL-QUESTION-what-is-the-ceo-salary"
SENTINEL_C = "SENTINEL-CHUNK-confidential-clause"
SENTINEL_A = "SENTINEL-ANSWER-sentence"
SENTINEL_T = "SENTINEL-TITLE-Secret-Handbook"
QUOTE = "Notice is thirty days."
CHUNK_BODY = f"{SENTINEL_C} {QUOTE}"
GOOD = answer_json(answer=f"{SENTINEL_A} {QUOTE}", citations=[{"ref": 1, "quote": QUOTE}])
BAD = answer_json(answer=f"{SENTINEL_A} 99 days.", citations=[{"ref": 1, "quote": QUOTE}])
QUESTION = f"{SENTINEL_Q}?"


def _rig(*steps: object, config: AskConfig | None = None, model: object | None = None) -> Rig:
    script = list(steps) if steps else [result_of(GOOD)]
    rig = build(script, model=model, config=config)  # type: ignore[arg-type]
    rig.store.chunks = [make_chunk(CHUNK_BODY, title=SENTINEL_T)]
    return rig


def _refused(reason: str) -> object:
    return result_of(answer_json(refused=True, reason=reason, answer=""))


def _scenarios() -> dict[str, Callable[[], Rig]]:
    def limiter() -> Rig:
        rig = _rig()
        rig.limiter.wait = 30
        return rig

    def flag() -> Rig:
        rig = _rig()
        rig.flag.value = True
        return rig

    def busy() -> Rig:
        rig = _rig()
        rig.in_flight.grant = False
        return rig

    def cap() -> Rig:
        rig = _rig()
        rig.store.state = TenantState(TenantAnswerSettings(("groq",), "fictional", 10), 10)
        return rig

    def not_configured() -> Rig:
        rig = _rig()
        rig.store.state = TenantState(None, 0)
        return rig

    def disallowed() -> Rig:
        rig = _rig()
        rig.store.state = TenantState(TenantAnswerSettings(("groq",), "real", 10_000), 0)
        return rig

    def no_key() -> Rig:
        rig = _rig()
        rig.providers.models.clear()
        return rig

    def state_down() -> Rig:
        rig = _rig()
        rig.store.read_error = StoreUnavailable(SENTINEL_Q)
        return rig

    def embed_down() -> Rig:
        rig = _rig()
        rig.embedder.error = QueryEmbedderNotReady(SENTINEL_Q)
        return rig

    def embed_busy() -> Rig:
        rig = _rig()
        rig.embedder.error = QueryEmbedderBusy(SENTINEL_Q)
        return rig

    def retrieve_down() -> Rig:
        rig = _rig()
        rig.store.retrieve_error = StoreUnavailable(SENTINEL_C)
        return rig

    def unexpected() -> Rig:
        rig = _rig()
        rig.store.retrieve_error = RuntimeError(f"{SENTINEL_Q} {SENTINEL_C} {SENTINEL_A}")
        return rig

    def metering_down() -> Rig:
        rig = _rig()
        rig.store.record_failures = 2
        return rig

    def provider_error() -> Rig:
        return _rig(*[ProviderUnavailable(SENTINEL_A)] * 4)

    def rate_limited() -> Rig:
        return _rig(ProviderRateLimited(SENTINEL_A, 3.0), ProviderRateLimited(SENTINEL_A, 3.0))

    def provider_timeout() -> Rig:
        return _rig(ProviderTimeout(SENTINEL_A), ProviderTimeout(SENTINEL_A))

    def slow() -> Rig:
        trace: list[str] = []
        return _rig(
            model=SlowModel(5.0, trace),
            config=AskConfig(deadline_seconds=0.3, metering_timeout=0.3),
        )

    return {
        "answered": lambda: _rig(),
        "retry_then_answered": lambda: _rig(result_of(BAD), result_of(GOOD)),
        "could_not_verify": lambda: _rig(result_of(BAD), result_of(BAD)),
        "refused_not_in_documents": lambda: _rig(_refused("not_in_documents")),
        "refused_needs_clarification": lambda: _rig(_refused("needs_clarification")),
        "refused_conflicting": lambda: _rig(_refused("conflicting_sources")),
        "rate_limited": limiter,
        "metering_flag": flag,
        "busy": busy,
        "daily_cap": cap,
        "not_configured": not_configured,
        "provider_disallowed": disallowed,
        "no_key": no_key,
        "state_down": state_down,
        "embed_not_ready": embed_down,
        "embed_busy": embed_busy,
        "retrieve_down": retrieve_down,
        "unexpected": unexpected,
        "metering_down": metering_down,
        "provider_error": provider_error,
        "provider_rate_limited": rate_limited,
        "provider_timeout": provider_timeout,
        "deadline": slow,
    }


def _text_of(record: logging.LogRecord) -> str:
    """Everything a log handler could write for this record: message, arguments, extras and the traceback."""
    formatted = logging.Formatter().format(record)
    extras = {
        key: value
        for key, value in record.__dict__.items()
        if key not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__
    }
    return f"{formatted} {record.args!r} {extras!r}"


@pytest.mark.parametrize("name", list(_scenarios()))
async def test_no_scenario_logs_or_stores_what_the_user_or_the_documents_said(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _scenarios()[name]()
    forbidden = [
        SENTINEL_Q,
        SENTINEL_C,
        SENTINEL_A,
        SENTINEL_T,
        str(rig.caller.tenant_id),
        rig.caller.tenant_id.hex,
        str(rig.caller.tenant_id).upper(),
        str(rig.caller.user_id),
        rig.caller.user_id.hex,
        str(rig.caller.user_id).upper(),
    ]
    request_id = str(uuid.uuid4())
    with caplog.at_level(logging.DEBUG):
        try:
            result = await rig.service.ask(rig.caller, QUESTION, request_id)
        except RuntimeError:
            result = (
                None  # the "unexpected" scenario: the error goes up, nothing about it is logged
            )

    logs = "\n".join(_text_of(record) for record in caplog.records)
    for value in forbidden:
        assert value not in logs, f"{name}: {value!r} reached a log record"
    for record in rig.store.records:
        assert not any(value in repr(record) for value in forbidden), (
            f"{name}: text in a metering record"
        )
    if result is not None and not isinstance(result, Answered):
        assert not any(value in repr(result) for value in forbidden), f"{name}: text in the result"

    # Not vacuous: the lines an operator needs are there, with the hash of the tenant and no raw id.
    tenant_ref = tenant_log_ref(LOG_KEY, rig.caller.tenant_id)
    assert request_id in logs
    assert tenant_ref in logs


async def test_the_provider_gets_the_question_and_chunk_text_and_nothing_else() -> None:
    rig = _rig()
    await rig.service.ask(rig.caller, QUESTION, "req-1")
    (request,) = rig.model.requests  # type: ignore[attr-defined]
    seen = repr(request)
    assert SENTINEL_Q in seen and SENTINEL_C in seen  # the model needs both
    for value in (
        SENTINEL_T,
        str(rig.caller.tenant_id),
        rig.caller.tenant_id.hex,
        str(rig.caller.user_id),
        rig.caller.user_id.hex,
    ):
        assert value not in seen


async def test_the_embedder_gets_only_the_question() -> None:
    rig = _rig()
    await rig.service.ask(rig.caller, QUESTION, "req-1")
    assert rig.embedder.texts == [QUESTION]

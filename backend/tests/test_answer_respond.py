"""
The answer path with a fake provider (ADR 0032): refusal rules, the one corrective retry, the final refusal, the
difference between a refusal and an error, and what is (not) logged.
"""

import logging

import pytest

from grounded_answers.answering import (
    AnswerServiceError,
    GroundedAnswer,
    InvalidOutput,
    ProviderRateLimited,
    ProviderRefused,
    ProviderRejected,
    ProviderTimeout,
    Refusal,
    answer_question,
)
from grounded_answers.answering.messages import REFUSAL_MESSAGES
from grounded_answers.answering.prompt import CORRECTIVE_NOTE
from grounded_answers.answering.schema import REFUSAL_REASONS
from tests.answering_support import Scripted, Sleeps, answer_json, make_chunk, result_of

pytestmark = pytest.mark.anyio

BODY = "3.2 Probation - 3.2.1 Probation is 3 months for all employees except drivers."
CHUNK = make_chunk(BODY, section_number="3", title="HR Leave Policy secret-title")
QUESTION = "How long is probation for new hires?"
QUOTE = "Probation is 3 months for all employees except drivers."
GOOD = answer_json(answer="Probation is 3 months.", citations=[{"ref": 1, "quote": QUOTE}])


async def ask(model: Scripted, chunks: list = [CHUNK]):  # type: ignore[no-untyped-def,type-arg]  # noqa: B006
    return await answer_question(model, QUESTION, chunks, sleep=Sleeps())


async def test_a_valid_answer_carries_the_chunk_the_quote_and_the_clause() -> None:
    outcome = await ask(Scripted([result_of(GOOD)]))
    assert isinstance(outcome.result, GroundedAnswer)
    assert outcome.attempts == 1 and outcome.invalid == () and outcome.review_signals == ()
    cited = outcome.result.citations[0]
    assert cited.chunk is CHUNK and cited.quote == QUOTE
    assert (cited.location.level, cited.location.label) == ("clause", "3.2.1")


async def test_no_chunks_is_a_refusal_and_the_model_is_not_called() -> None:
    model = Scripted([])
    outcome = await answer_question(model, QUESTION, [], sleep=Sleeps())
    assert isinstance(outcome.result, Refusal) and outcome.result.kind == "not_in_documents"
    assert model.calls == 0 and outcome.attempts == 0 and outcome.usage.input_tokens == 0


@pytest.mark.parametrize("reason", REFUSAL_REASONS)
async def test_a_model_refusal_shows_our_wording_and_is_not_retried(reason: str) -> None:
    model = Scripted([result_of(answer_json(refused=True, reason=reason, answer="MODEL WORDS"))])
    outcome = await ask(model)
    assert isinstance(outcome.result, Refusal)
    assert outcome.result.message == REFUSAL_MESSAGES[reason]
    assert "MODEL WORDS" not in outcome.result.message
    assert model.calls == 1 and outcome.review_signals == ()


async def test_a_refusal_ignores_any_citations_the_model_sent_with_it() -> None:
    reply = answer_json(
        refused=True, reason="not_in_documents", answer="", citations=[{"ref": 9, "quote": "x"}]
    )
    outcome = await ask(Scripted([result_of(reply)]))
    assert isinstance(outcome.result, Refusal) and outcome.attempts == 1


async def test_refused_with_reason_none_counts_as_not_in_documents() -> None:
    outcome = await ask(Scripted([result_of(answer_json(refused=True, reason="none", answer=""))]))
    assert isinstance(outcome.result, Refusal) and outcome.result.kind == "not_in_documents"


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (answer_json(citations=[{"ref": 7, "quote": QUOTE}]), "bad_ref"),
        (
            answer_json(citations=[{"ref": 1, "quote": "a quote that the chunk never contained"}]),
            "quote_not_found",
        ),
        (answer_json(citations=[]), "no_citations"),
        (
            answer_json(answer="Probation is 9 months.", citations=[{"ref": 1, "quote": QUOTE}]),
            "number_not_grounded",
        ),
    ],
)
async def test_an_invalid_answer_gets_one_corrective_retry_that_can_succeed(
    reply: str, code: str
) -> None:
    model = Scripted([result_of(reply), result_of(GOOD)])
    outcome = await ask(model)
    assert isinstance(outcome.result, GroundedAnswer)
    assert (outcome.attempts, outcome.invalid) == (2, (code,))
    assert outcome.review_signals == ("retried", code)
    assert [r.corrective for r in model.requests] == [False, True]
    # Both attempts are billed.
    assert outcome.usage.input_tokens == 1000 + 1000


async def test_invalid_twice_is_a_could_not_verify_refusal_and_never_shows_the_broken_answer() -> (
    None
):
    bad = answer_json(answer="BROKEN ANSWER 99", citations=[{"ref": 7, "quote": "q"}])
    model = Scripted([result_of(bad), result_of(bad)])
    outcome = await ask(model)
    assert isinstance(outcome.result, Refusal) and outcome.result.kind == "could_not_verify"
    assert "BROKEN" not in outcome.result.message
    assert (outcome.attempts, outcome.invalid, model.calls) == (2, ("bad_ref", "bad_ref"), 2)
    assert "could_not_verify" in outcome.review_signals


async def test_malformed_output_is_retried_then_refused() -> None:
    model = Scripted([InvalidOutput("the output is not JSON"), InvalidOutput("cut off")])
    outcome = await ask(model)
    assert isinstance(outcome.result, Refusal) and outcome.result.kind == "could_not_verify"
    assert outcome.invalid == ("invalid_output", "invalid_output") and model.calls == 2
    assert outcome.usage.input_tokens == 0  # an unparsable reply has no usage to count


async def test_malformed_output_then_a_good_retry_is_an_answer() -> None:
    model = Scripted([InvalidOutput("not JSON"), result_of(GOOD)])
    outcome = await ask(model)
    assert isinstance(outcome.result, GroundedAnswer) and outcome.attempts == 2


async def test_the_retry_prompt_has_the_fixed_note_and_nothing_from_the_bad_reply() -> None:
    from grounded_answers.answering.prompt import build_prompt

    bad = answer_json(answer="LEAKY BAD ANSWER", citations=[{"ref": 7, "quote": "LEAKY QUOTE"}])
    model = Scripted([result_of(bad), result_of(GOOD)])
    await ask(model)
    retry_prompt = build_prompt(model.requests[1]).user
    assert retry_prompt.endswith(CORRECTIVE_NOTE)
    assert "LEAKY" not in retry_prompt


async def test_a_provider_safety_block_is_a_refusal_not_an_error_and_is_not_retried() -> None:
    model = Scripted([ProviderRefused("blocked")])
    outcome = await ask(model)
    assert isinstance(outcome.result, Refusal) and outcome.result.kind == "declined"
    assert model.calls == 1


@pytest.mark.parametrize(
    "error",
    [ProviderTimeout("t"), ProviderRateLimited("r", 5000.0), ProviderRejected("bad key", 401)],
)
async def test_a_provider_failure_is_an_error_not_a_refusal(error: Exception) -> None:
    with pytest.raises(AnswerServiceError) as caught:
        await ask(Scripted([error] * 4))
    assert isinstance(caught.value.__cause__, type(error))
    assert str(caught.value) == type(error).__name__


async def test_network_failures_do_not_use_up_the_invalid_output_retry() -> None:
    from grounded_answers.answering import ProviderUnavailable

    model = Scripted([ProviderUnavailable("x"), result_of(GOOD)])
    outcome = await ask(model)
    assert isinstance(outcome.result, GroundedAnswer)
    assert outcome.attempts == 1 and outcome.invalid == ()


async def test_no_log_line_holds_the_question_the_answer_a_quote_or_a_title(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    bad = answer_json(answer="SECRET ANSWER 99", citations=[{"ref": 7, "quote": "SECRET QUOTE"}])
    await ask(Scripted([result_of(bad), result_of(GOOD)]))
    await ask(Scripted([result_of(bad), result_of(bad)]))
    text = caplog.text + repr([r.__dict__ for r in caplog.records])
    for secret in (QUESTION, "SECRET ANSWER", "SECRET QUOTE", "secret-title", QUOTE, BODY[:30]):
        assert secret not in text
    outcomes = [r.answer_outcome for r in caplog.records if r.getMessage() == "answer outcome"]  # type: ignore[attr-defined]
    assert outcomes == ["answered", "could_not_verify"]
    codes = [r.answer_invalid_codes for r in caplog.records if r.getMessage() == "answer outcome"]  # type: ignore[attr-defined]
    assert codes == [["bad_ref"], ["bad_ref", "bad_ref"]]


def test_every_refusal_kind_has_our_own_message() -> None:
    kinds = {*REFUSAL_REASONS, "could_not_verify", "declined"}
    assert kinds <= set(REFUSAL_MESSAGES)
    assert all(message and message[0].isupper() for message in REFUSAL_MESSAGES.values())

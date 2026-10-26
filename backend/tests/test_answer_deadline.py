"""
answer_question with a deadline (ADR 0033, decision 6): the per-attempt timeout, the network retries, the corrective
retry, the usage that survives a failure, and the hard stop. Without a deadline nothing changes.
"""

import pytest

from grounded_answers.answering import answer_question
from grounded_answers.answering.respond import (
    ATTEMPT_TIMEOUT_SECONDS,
    AnswerDeadlineExceeded,
    AnswerServiceError,
)
from grounded_answers.answering.types import (
    AnswerResult,
    ProviderRateLimited,
    ProviderUnavailable,
)
from tests.answering_support import Scripted, Sleeps, answer_json, make_chunk, result_of

pytestmark = pytest.mark.anyio

CHUNKS = [make_chunk("Notice is thirty days.")]
GOOD = answer_json()
BAD = answer_json(
    answer="Notice is 99 days."
)  # the number is in no quote: invalid, so a retry is asked for
NOW = 1000.0


def _clock() -> float:
    return NOW


async def test_without_a_deadline_the_defaults_stand() -> None:
    model = Scripted([result_of(GOOD)])
    await answer_question(model, "q?", CHUNKS)
    assert model.requests[0].timeout_seconds == 45.0


@pytest.mark.parametrize(("remaining", "expected"), [(20.0, ATTEMPT_TIMEOUT_SECONDS), (5.0, 5.0)])
async def test_an_attempt_gets_the_smaller_of_eight_seconds_and_the_time_left(
    remaining: float, expected: float
) -> None:
    model = Scripted([result_of(GOOD)])
    await answer_question(model, "q?", CHUNKS, deadline=NOW + remaining, clock=_clock)
    assert model.requests[0].timeout_seconds == expected


async def test_a_deadline_already_passed_raises_with_no_usage_and_no_call() -> None:
    model = Scripted([result_of(GOOD)])
    with pytest.raises(AnswerDeadlineExceeded) as caught:
        await answer_question(model, "q?", CHUNKS, deadline=NOW - 1, clock=_clock)
    assert caught.value.attempts == 0 and caught.value.usage.input_tokens == 0
    assert model.calls == 0


async def test_the_corrective_retry_is_skipped_under_four_seconds_left() -> None:
    model = Scripted([result_of(BAD), result_of(GOOD)])
    outcome = await answer_question(model, "q?", CHUNKS, deadline=NOW + 3.0, clock=_clock)
    assert model.calls == 1
    assert outcome.result.kind == "could_not_verify"  # type: ignore[union-attr]
    assert outcome.attempts == 1
    assert outcome.invalid == ("number_not_grounded",)


async def test_the_corrective_retry_runs_with_four_seconds_or_more_left() -> None:
    model = Scripted([result_of(BAD), result_of(GOOD)])
    outcome = await answer_question(model, "q?", CHUNKS, deadline=NOW + 4.0, clock=_clock)
    assert model.calls == 2 and outcome.attempts == 2


async def test_network_failures_get_two_calls_with_a_deadline_and_four_without() -> None:
    with_deadline = Scripted([ProviderUnavailable("x")] * 4)
    with pytest.raises(AnswerServiceError):
        await answer_question(
            with_deadline, "q?", CHUNKS, deadline=NOW + 20, clock=_clock, sleep=Sleeps()
        )
    assert with_deadline.calls == 2

    without = Scripted([ProviderUnavailable("x")] * 4)
    with pytest.raises(AnswerServiceError):
        await answer_question(without, "q?", CHUNKS, sleep=Sleeps())
    assert without.calls == 4


async def test_a_provider_wait_longer_than_the_time_left_is_not_slept() -> None:
    sleeps = Sleeps()
    model = Scripted([ProviderRateLimited("slow", 30.0), result_of(GOOD)])
    with pytest.raises(AnswerServiceError):
        await answer_question(model, "q?", CHUNKS, deadline=NOW + 10, clock=_clock, sleep=sleeps)
    assert model.calls == 1 and sleeps.waits == []

    short = Sleeps()
    ok = Scripted([ProviderRateLimited("slow", 3.0), result_of(GOOD)])
    outcome = await answer_question(ok, "q?", CHUNKS, deadline=NOW + 10, clock=_clock, sleep=short)
    assert short.waits == [3.0] and outcome.attempts == 1


async def test_a_failure_carries_the_usage_and_attempts_so_far() -> None:
    model = Scripted([result_of(BAD), ProviderUnavailable("x"), ProviderUnavailable("x")])
    with pytest.raises(AnswerServiceError) as caught:
        await answer_question(model, "q?", CHUNKS, deadline=NOW + 20, clock=_clock, sleep=Sleeps())
    assert caught.value.attempts == 2
    assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (1000, 100)
    assert isinstance(caught.value.__cause__, ProviderUnavailable)


async def test_a_failure_without_a_deadline_also_carries_usage() -> None:
    script: list[AnswerResult | Exception] = [result_of(BAD)] + [ProviderUnavailable("x")] * 4
    model = Scripted(script)
    with pytest.raises(AnswerServiceError) as caught:
        await answer_question(model, "q?", CHUNKS, sleep=Sleeps())
    assert caught.value.usage.input_tokens == 1000


async def test_a_call_that_outlasts_the_deadline_is_stopped() -> None:
    import asyncio
    import time

    class Hangs:
        provider = "groq"
        model = "openai/gpt-oss-120b"

        async def answer(self, request: object) -> object:
            await asyncio.sleep(30)
            raise AssertionError("not reached")

    started = time.monotonic()
    with pytest.raises(AnswerDeadlineExceeded):
        await answer_question(Hangs(), "q?", CHUNKS, deadline=time.monotonic() + 0.2)  # type: ignore[arg-type]
    assert time.monotonic() - started < 3

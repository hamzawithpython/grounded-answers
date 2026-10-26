"""Retries (outside the adapters) and the provider test runner, with a fake model: no network, no waiting."""

import pytest

from grounded_answers.answering import (
    AnswerRequest,
    AnswerResult,
    InvalidOutput,
    ModelAnswer,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    SourceChunk,
    Usage,
    answer_with_retries,
)
from grounded_answers.answering.pricing import UnknownModelPrice, list_price_usd
from grounded_answers.evaluation.answer_run import (
    AnswerItem,
    render_markdown,
    run_provider,
    select_items,
    summarize,
    to_json,
)

pytestmark = pytest.mark.anyio

REQUEST = AnswerRequest("q", (SourceChunk(1, "t"),))


def result(
    *,
    answered: bool = True,
    tokens_in: int = 1000,
    tokens_out: int = 100,
    quota: dict[str, str] | None = None,
) -> AnswerResult:
    return AnswerResult(
        ModelAnswer(answered, "a", (1,) if answered else ()),
        Usage(tokens_in, tokens_out, 30),
        latency_ms=500.0,
        provider="fake",
        model="openai/gpt-oss-120b",
        quota=quota or {},
    )


class Scripted:
    """A model that plays a list of results or errors, one per call."""

    provider = "fake"
    model = "openai/gpt-oss-120b"

    def __init__(self, script: list[AnswerResult | Exception]) -> None:
        self.script = list(script)
        self.calls = 0

    async def answer(self, request: AnswerRequest) -> AnswerResult:
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


# --- retries ------------------------------------------------------------------------------------------------


async def test_a_rate_limit_waits_for_the_hint_then_succeeds() -> None:
    sleeps = Sleeps()
    model = Scripted([ProviderRateLimited("429", 7.0), result()])
    assert (await answer_with_retries(model, REQUEST, sleep=sleeps)).provider == "fake"
    assert sleeps.waits == [7.0]


async def test_a_long_hint_means_a_daily_quota_and_is_raised_at_once() -> None:
    sleeps = Sleeps()
    model = Scripted([ProviderRateLimited("429", 3600.0)])
    with pytest.raises(ProviderRateLimited):
        await answer_with_retries(model, REQUEST, sleep=sleeps)
    assert sleeps.waits == [] and model.calls == 1


async def test_outages_and_timeouts_back_off_then_give_up() -> None:
    sleeps = Sleeps()
    model = Scripted(
        [
            ProviderUnavailable("x"),
            ProviderTimeout("x"),
            ProviderUnavailable("x"),
            ProviderUnavailable("x"),
        ]
    )
    with pytest.raises(ProviderUnavailable):
        await answer_with_retries(model, REQUEST, sleep=sleeps)
    assert sleeps.waits == [1.0, 2.0, 4.0] and model.calls == 4


@pytest.mark.parametrize("error", [ProviderRejected("no", 400), InvalidOutput("bad")])
async def test_a_request_that_would_fail_the_same_way_is_not_retried(error: Exception) -> None:
    model = Scripted([error, result()])
    with pytest.raises(type(error)):
        await answer_with_retries(model, REQUEST, sleep=Sleeps())
    assert model.calls == 1


# --- price --------------------------------------------------------------------------------------------------


def test_list_price_uses_input_and_output_rates() -> None:
    assert list_price_usd(
        "openai/gpt-oss-120b", Usage(1_000_000, 1_000_000, None)
    ) == pytest.approx(0.75)
    assert list_price_usd("claude-haiku-5-5", Usage(1100, 200, None)) == pytest.approx(0.00021)
    with pytest.raises(UnknownModelPrice):
        list_price_usd("nope", Usage(1, 1, None))


# --- runner -------------------------------------------------------------------------------------------------


def items(n: int) -> list[AnswerItem]:
    return [AnswerItem(f"q{i:03d}", "q", i % 5 != 4, (SourceChunk(1, "t"),)) for i in range(n)]


def test_select_items_spreads_the_sample_across_the_list() -> None:
    chosen = [i.question_id for i in select_items(items(50), 5)]
    assert chosen == ["q000", "q010", "q020", "q030", "q040"]
    assert len(select_items(items(3), 10)) == 3


async def test_the_runner_paces_calls_and_totals_tokens_cost_and_quota() -> None:
    sleeps = Sleeps()
    model = Scripted(
        [result(quota={"x-ratelimit-remaining-tokens": "5000"}), result(answered=False)]
    )
    run = await run_provider(model, items(2), pace_seconds=12.0, token_budget=10_000, sleep=sleeps)
    summary = summarize(run)
    assert sleeps.waits == [12.0]
    assert (summary["calls"], summary["ok"], summary["stopped"]) == (2, 2, None)
    assert (summary["tokens_input"], summary["tokens_output"], summary["tokens_reasoning"]) == (
        2000,
        200,
        60,
    )
    assert summary["cost_usd_mean"] == pytest.approx((1000 * 0.15 + 100 * 0.60) / 1_000_000)
    assert summary["meets_cost_target"] is True and summary["meets_latency_target"] is True
    assert summary["quota_headers"] == {"x-ratelimit-remaining-tokens": "5000"}
    assert "x-ratelimit-remaining-tokens" in render_markdown([run])


async def test_the_runner_stops_at_the_token_budget() -> None:
    model = Scripted([result(), result(), result()])
    run = await run_provider(model, items(3), pace_seconds=0, token_budget=1500, sleep=Sleeps())
    assert len(run.records) == 2
    assert run.stopped == "token budget of 1500 reached"


async def test_a_rejected_call_stops_the_run_so_it_does_not_burn_quota() -> None:
    model = Scripted([ProviderRejected("groq: HTTP 400: schema not supported", 400), result()])
    run = await run_provider(model, items(3), pace_seconds=0, token_budget=10_000, sleep=Sleeps())
    assert model.calls == 1
    assert run.stopped is not None and "HTTP 400" in run.stopped


async def test_other_failures_are_counted_and_the_run_goes_on() -> None:
    model = Scripted([InvalidOutput("cut off"), result()])
    run = await run_provider(model, items(2), pace_seconds=0, token_budget=10_000, sleep=Sleeps())
    summary = summarize(run)
    assert (summary["ok"], summary["errors"]) == (1, {"InvalidOutput": 1})


async def test_the_json_report_has_numbers_and_ids_but_no_text() -> None:
    run = await run_provider(
        Scripted([result()]), items(1), pace_seconds=0, token_budget=10_000, sleep=Sleeps()
    )
    record = to_json([run])[0]["records"][0]
    assert set(record) == {
        "question",
        "error",
        "answered",
        "citations",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "latency_ms",
        "cost_usd",
    }

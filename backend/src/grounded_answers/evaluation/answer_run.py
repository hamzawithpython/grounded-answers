"""
The provider test (Session 5, Step 3b): send retrieved sources and questions to an answer model and record what
each call used. Tokens, cost and latency go in the report only; there is no metering table yet (that is Step 5).

This measures whether a provider works, what it costs and how fast it is. It does not judge answer quality: the
answered/refused counts are a sanity check, not a score. Quality (citations, refusals) is Steps 4 and 7.

A run can be small on purpose. The free tiers have daily limits, so a run takes `limit` evenly spaced questions,
paces its calls, and stops at a token budget. The report says what was used and what the provider said was left.
The report holds numbers and question ids. It never holds a question, a source, an answer or a key.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from grounded_answers.answering import (
    AnswerModel,
    AnswerModelError,
    AnswerRequest,
    ProviderRateLimited,
    ProviderRejected,
    SourceChunk,
    answer_with_retries,
)
from grounded_answers.answering.pricing import list_price_usd
from grounded_answers.evaluation.scoring import percentile

COST_TARGET_USD: Final = 0.002
LATENCY_TARGET_MS: Final = 8000.0


@dataclass(frozen=True)
class AnswerItem:
    question_id: str
    text: str
    answerable: bool
    sources: tuple[SourceChunk, ...]


@dataclass(frozen=True)
class CallRecord:
    question_id: str
    answerable: bool
    # None when the call succeeded; otherwise the error class name.
    error: str | None
    answered: bool | None = None
    citations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None
    latency_ms: float = 0.0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class ProviderRun:
    provider: str
    model: str
    records: tuple[CallRecord, ...]
    # Why the run ended early, or None when every selected question was asked.
    stopped: str | None
    quota: Mapping[str, str] = field(default_factory=dict)


def select_items(items: Sequence[AnswerItem], limit: int) -> list[AnswerItem]:
    """`limit` items spread evenly across the list, so a small run still mixes the question types."""
    if limit >= len(items):
        return list(items)
    return [items[i * len(items) // limit] for i in range(limit)]


async def run_provider(
    model: AnswerModel,
    items: Sequence[AnswerItem],
    *,
    pace_seconds: float,
    token_budget: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ProviderRun:
    records: list[CallRecord] = []
    used = 0
    stopped: str | None = None
    quota: Mapping[str, str] = {}
    for index, item in enumerate(items):
        if used >= token_budget:
            stopped = f"token budget of {token_budget} reached"
            break
        if index:
            await sleep(pace_seconds)
        try:
            result = await answer_with_retries(
                model, AnswerRequest(item.text, item.sources), sleep=sleep
            )
        except ProviderRejected as error:
            # A bad key, an unknown model or an unsupported schema fails every call the same way.
            records.append(CallRecord(item.question_id, item.answerable, type(error).__name__))
            stopped = str(error)
            break
        except ProviderRateLimited as error:
            records.append(CallRecord(item.question_id, item.answerable, type(error).__name__))
            stopped = f"{error} (daily quota, or retries used up)"
            break
        except AnswerModelError as error:
            records.append(CallRecord(item.question_id, item.answerable, type(error).__name__))
            continue
        used += result.usage.input_tokens + result.usage.output_tokens
        quota = result.quota or quota
        records.append(
            CallRecord(
                item.question_id,
                item.answerable,
                None,
                answered=result.answer.answered,
                citations=len(result.answer.citations),
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                reasoning_tokens=result.usage.reasoning_tokens,
                latency_ms=result.latency_ms,
                cost_usd=list_price_usd(model.model, result.usage),
            )
        )
    return ProviderRun(model.provider, model.model, tuple(records), stopped, quota)


def summarize(run: ProviderRun) -> dict[str, Any]:
    ok = [r for r in run.records if r.error is None]
    errors: dict[str, int] = {}
    for r in run.records:
        if r.error is not None:
            errors[r.error] = errors.get(r.error, 0) + 1
    out: dict[str, Any] = {
        "provider": run.provider,
        "model": run.model,
        "calls": len(run.records),
        "ok": len(ok),
        "errors": errors,
        "stopped": run.stopped,
        "quota_headers": dict(run.quota),
    }
    if not ok:
        return out
    reasoning = [r.reasoning_tokens for r in ok]
    latency = [r.latency_ms for r in ok]
    cost = [r.cost_usd for r in ok]
    answerable = [r for r in ok if r.answerable]
    unanswerable = [r for r in ok if not r.answerable]
    out |= {
        "tokens_input": sum(r.input_tokens for r in ok),
        "tokens_output": sum(r.output_tokens for r in ok),
        "tokens_reasoning": (
            sum(t for t in reasoning if t is not None) if None not in reasoning else None
        ),
        "tokens_per_answer_mean": sum(r.input_tokens + r.output_tokens for r in ok) / len(ok),
        "output_per_answer_mean": sum(r.output_tokens for r in ok) / len(ok),
        "latency_ms_p50": percentile(latency, 50),
        "latency_ms_p95": percentile(latency, 95),
        "cost_usd_mean": sum(cost) / len(cost),
        "cost_usd_p95": percentile(cost, 95),
        "cost_usd_total": sum(cost),
        "meets_latency_target": percentile(latency, 95) <= LATENCY_TARGET_MS,
        "meets_cost_target": percentile(cost, 95) <= COST_TARGET_USD,
        "answerable_asked": len(answerable),
        "answerable_answered": sum(1 for r in answerable if r.answered),
        "unanswerable_asked": len(unanswerable),
        "unanswerable_refused": sum(1 for r in unanswerable if r.answered is False),
    }
    return out


def to_json(runs: Sequence[ProviderRun]) -> list[dict[str, Any]]:
    return [
        summarize(run)
        | {
            "records": [
                {
                    "question": r.question_id,
                    "error": r.error,
                    "answered": r.answered,
                    "citations": r.citations,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "reasoning_tokens": r.reasoning_tokens,
                    "latency_ms": round(r.latency_ms, 1),
                    "cost_usd": round(r.cost_usd, 6),
                }
                for r in run.records
            ]
        }
        for run in runs
    ]


def render_markdown(runs: Sequence[ProviderRun]) -> str:
    lines = ["## Answer providers (Step 3b): works, costs, speed. Not a quality score.", ""]
    for run in runs:
        s = summarize(run)
        lines.append(f"### {s['provider']} / {s['model']}")
        lines.append(f"- Calls {s['calls']}, ok {s['ok']}, errors {s['errors'] or 'none'}")
        if s["stopped"]:
            lines.append(f"- Stopped early: {s['stopped']}")
        if s["ok"]:
            reasoning = s["tokens_reasoning"]
            lines += [
                f"- Tokens: input {s['tokens_input']}, output {s['tokens_output']} "
                f"(reasoning {reasoning if reasoning is not None else 'not reported'}); "
                f"{s['tokens_per_answer_mean']:.0f} per answer, output {s['output_per_answer_mean']:.0f} per answer",
                f"- Latency p50 {s['latency_ms_p50']:.0f} ms, p95 {s['latency_ms_p95']:.0f} ms "
                f"(target {LATENCY_TARGET_MS:.0f} ms: {'met' if s['meets_latency_target'] else 'NOT met'}; "
                "one successful call, without rate-limit waits)",
                f"- List-price cost per answer: mean ${s['cost_usd_mean']:.5f}, p95 ${s['cost_usd_p95']:.5f} "
                f"(target ${COST_TARGET_USD}: {'met' if s['meets_cost_target'] else 'NOT met'}); "
                f"run total ${s['cost_usd_total']:.5f}. The free tier charges nothing.",
                f"- Sanity: answered {s['answerable_answered']} of {s['answerable_asked']} answerable; "
                f"refused {s['unanswerable_refused']} of {s['unanswerable_asked']} unanswerable (not a quality score)",
            ]
        if s["quota_headers"]:
            lines.append(f"- Rate-limit headers on the last response: {s['quota_headers']}")
        lines.append("")
    return "\n".join(lines)

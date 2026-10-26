"""
Fakes for the POST /ask flow (ADR 0033): every interface of ask/types.py, all writing to one shared trace so a test
can assert the order of the stages and what was never called.
"""

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from grounded_answers.answering.types import AnswerRequest, AnswerResult
from grounded_answers.ask.service import AskConfig, AskService
from grounded_answers.ask.types import (
    Caller,
    StoreUnavailable,
    TenantAnswerSettings,
    TenantState,
    UsageRecord,
)
from grounded_answers.rate_limit import LimiterUnavailable
from grounded_answers.retrieval import RetrievedChunk
from tests.answering_support import Scripted, answer_json, make_chunk, result_of

LOG_KEY = b"k" * 48
CAPTION = "Notice is thirty days."
GOOD_REPLY = answer_json(answer="Notice is thirty days.", citations=[{"ref": 1, "quote": CAPTION}])
NOW = datetime(2026, 10, 26, 23, 0, 0, tzinfo=UTC)  # one hour before the daily cap resets


async def no_sleep(seconds: float) -> None:
    """The retry helper's waits cost no real time in these tests."""


class GroqLike(Scripted):
    """A scripted model that reports itself as a provider the metering table accepts."""

    provider = "groq"


class SlowModel:
    """A model that waits (a real sleep) before answering, and says when it started."""

    provider = "groq"
    model = "openai/gpt-oss-120b"

    def __init__(self, delay: float, trace: list[str]) -> None:
        self.delay = delay
        self.trace = trace
        self.started = asyncio.Event()
        self.requests: list[AnswerRequest] = []

    async def answer(self, request: AnswerRequest) -> AnswerResult:
        self.requests.append(request)
        self.trace.append("model")
        self.started.set()
        await asyncio.sleep(self.delay)
        return result_of(GOOD_REPLY)


class FakeLimiter:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.wait: int | None = None
        self.down = False

    async def admit(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> int | None:
        self.trace.append("limiter")
        if self.down:
            raise LimiterUnavailable
        return self.wait


class FakeInFlight:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.grant = True
        self.down = False
        self.release_down = False
        self.held: set[str] = set()

    async def acquire(self, tenant_id: uuid.UUID, request_id: str) -> bool:
        self.trace.append("inflight.acquire")
        if self.down:
            raise LimiterUnavailable
        if self.grant:
            self.held.add(request_id)
        return self.grant

    async def release(self, tenant_id: uuid.UUID, request_id: str) -> None:
        self.trace.append("inflight.release")
        if self.release_down:
            raise LimiterUnavailable
        self.held.discard(request_id)


class FakeFlag:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.value = False
        self.read_down = False
        self.set_down = False
        self.set_calls = 0

    async def is_set(self, tenant_id: uuid.UUID) -> bool:
        self.trace.append("flag.read")
        if self.read_down:
            raise LimiterUnavailable
        return self.value

    async def set(self, tenant_id: uuid.UUID) -> None:
        self.trace.append("flag.set")
        self.set_calls += 1
        if self.set_down:
            raise LimiterUnavailable
        self.value = True


class FakeStore:
    """Counts how many store methods are running at once: the model must never be called while one is."""

    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.state = TenantState(TenantAnswerSettings(("groq",), "fictional", 50_000), 0)
        self.chunks: list[RetrievedChunk] = [make_chunk(CAPTION)]
        self.read_error: Exception | None = None
        self.retrieve_error: Exception | None = None
        self.read_delay = 0.0
        self.record_failures = 0
        self.record_delay = 0.0
        self.records: list[UsageRecord] = []
        self.record_attempts = 0
        self.active = 0

    async def read_state(self, caller: Caller) -> TenantState:
        self.trace.append("store.read_state")
        self.active += 1
        try:
            await asyncio.sleep(self.read_delay)
            if self.read_error is not None:
                raise self.read_error
            return self.state
        finally:
            self.active -= 1

    async def retrieve(
        self, caller: Caller, question: str, vector: list[float], top_k: int
    ) -> list[RetrievedChunk]:
        self.trace.append("store.retrieve")
        self.active += 1
        try:
            if self.retrieve_error is not None:
                raise self.retrieve_error
            return list(self.chunks)
        finally:
            self.active -= 1

    async def record(self, caller: Caller, record: UsageRecord) -> None:
        self.trace.append("store.record")
        self.active += 1
        try:
            self.record_attempts += 1
            await asyncio.sleep(self.record_delay)
            if self.record_failures > 0:
                self.record_failures -= 1
                raise StoreUnavailable
            self.records.append(record)
        finally:
            self.active -= 1


class FakeEmbedder:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.error: Exception | None = None
        self.delay = 0.0
        self.texts: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.trace.append("embed")
        self.texts.append(text)
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return [0.0, 1.0]


class FakeProviders:
    def __init__(self, models: dict[str, object]) -> None:
        self.models = models

    def get(self, name: str) -> object | None:
        return self.models.get(name)


@dataclass
class Rig:
    service: AskService
    trace: list[str]
    limiter: FakeLimiter
    in_flight: FakeInFlight
    flag: FakeFlag
    store: FakeStore
    embedder: FakeEmbedder
    model: object
    providers: FakeProviders
    caller: Caller = field(default_factory=lambda: Caller(uuid.uuid4(), uuid.uuid4()))


def build(
    script: Sequence[AnswerResult | Exception] | None = None,
    *,
    model: object | None = None,
    config: AskConfig | None = None,
) -> Rig:
    trace: list[str] = []
    scripted = model if model is not None else GroqLike(script or [result_of(GOOD_REPLY)])

    class Traced:
        """Records each model call in the shared trace, then delegates."""

        provider = "groq"

        def __init__(self, inner: object) -> None:
            self.inner = inner
            self.model = getattr(inner, "model")  # noqa: B009 - the fakes all define it

        async def answer(self, request: AnswerRequest) -> AnswerResult:
            if not isinstance(self.inner, SlowModel):
                trace.append("model")
            store_running = rig.store.active
            assert store_running == 0, "a store method was running during the model call"
            return await self.inner.answer(request)  # type: ignore[attr-defined,no-any-return]

    traced = Traced(scripted)
    limiter, in_flight, flag = FakeLimiter(trace), FakeInFlight(trace), FakeFlag(trace)
    store, embedder = FakeStore(trace), FakeEmbedder(trace)
    providers = FakeProviders({"groq": traced})
    service = AskService(
        limiter=limiter,
        in_flight=in_flight,
        flag=flag,
        store=store,
        embedder=embedder,
        providers=providers,  # type: ignore[arg-type]
        log_key=LOG_KEY,
        config=config or AskConfig(metering_timeout=0.3),
        now_utc=lambda: NOW,
        sleep=no_sleep,
    )
    rig = Rig(service, trace, limiter, in_flight, flag, store, embedder, scripted, providers)
    return rig

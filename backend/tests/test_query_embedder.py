"""
The query embedder of the web process (ADR 0033, decision 7): background load, readiness, bounded concurrency,
timeouts. Fake models only; the real model is measured in grounded_answers.evaluation.embed_concurrency.

Threads stand in for the model's CPU work: a fake blocks on a threading.Event, so the tests control exactly when a
"query" finishes without sleeping.
"""

import asyncio
import logging
import threading
import time
from pathlib import Path

import pytest

from grounded_answers.config import Settings
from grounded_answers.embedding.model import ModelIntegrityError
from grounded_answers.embedding.query import (
    FAILED,
    LOADING,
    READY,
    QueryEmbedder,
    QueryEmbedderBusy,
    QueryEmbedderNotReady,
    QueryEmbedderTimeout,
)
from grounded_answers.main import build_query_embedder
from grounded_answers.readiness import (
    EMBEDDER,
    EmbedderNotReadyError,
    ReadinessChecker,
    make_embedder_check,
)

pytestmark = pytest.mark.anyio

VECTOR = [0.0, 1.0]


class GatedModel:
    """A model whose queries wait for a gate, and which records how many run at once."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.calls = 0

    def query_embedding(self, text: str) -> list[float]:
        with self.lock:
            self.calls += 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            assert self.gate.wait(timeout=10), "the test never opened the gate"
            return VECTOR
        finally:
            with self.lock:
                self.running -= 1


class FailingModel:
    def query_embedding(self, text: str) -> list[float]:
        raise RuntimeError("model blew up")


async def _wait_for(state_of: QueryEmbedder, wanted: str) -> None:
    async with asyncio.timeout(5):
        while state_of.state != wanted:
            await asyncio.sleep(0.005)


async def _ready(model: GatedModel | FailingModel, **options: float) -> QueryEmbedder:
    embedder = QueryEmbedder(
        lambda: model, concurrency=int(options.pop("concurrency", 2)), **options
    )
    embedder.start()
    await _wait_for(embedder, READY)
    return embedder


# ---- loading ----


async def test_starts_loading_and_refuses_until_the_load_finishes() -> None:
    release = threading.Event()
    model = GatedModel()
    model.gate.set()

    def slow_loader() -> GatedModel:
        assert release.wait(timeout=10)
        return model

    embedder = QueryEmbedder(slow_loader, concurrency=2)
    assert embedder.state == LOADING
    embedder.start()
    with pytest.raises(QueryEmbedderNotReady):
        await embedder.embed("a question")
    release.set()
    await _wait_for(embedder, READY)
    assert await embedder.embed("a question") == VECTOR


async def test_a_failed_load_is_a_state_and_logs_only_the_error_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_dir = "C:/private/models/does-not-exist"

    def loader() -> GatedModel:
        raise ModelIntegrityError(f"model file missing: {private_dir}")

    embedder = QueryEmbedder(loader, concurrency=2)
    with caplog.at_level(logging.INFO):
        embedder.start()
        await _wait_for(embedder, FAILED)
    assert "reason=ModelIntegrityError" in caplog.text
    assert private_dir not in caplog.text
    with pytest.raises(QueryEmbedderNotReady):
        await embedder.embed("a question")


async def test_start_twice_is_an_error_and_close_cancels_a_pending_load() -> None:
    release = threading.Event()

    def loader() -> GatedModel:
        release.wait(timeout=10)
        return GatedModel()

    embedder = QueryEmbedder(loader, concurrency=1)
    embedder.start()
    with pytest.raises(RuntimeError, match="already started"):
        embedder.start()
    await embedder.aclose()
    release.set()
    assert embedder.state == LOADING  # never became ready: nothing was waiting for the result


def test_concurrency_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        QueryEmbedder(GatedModel, concurrency=0)


# ---- bounded concurrency ----


async def test_never_more_than_the_limit_run_at_once() -> None:
    model = GatedModel()
    embedder = await _ready(model, concurrency=2, slot_timeout=5, embed_timeout=5)
    tasks = [asyncio.create_task(embedder.embed(f"q{i}")) for i in range(6)]
    async with asyncio.timeout(5):
        while model.running < 2:
            await asyncio.sleep(0.005)
    await asyncio.sleep(0.05)  # time for a wrong implementation to start a third
    assert model.max_running == 2
    model.gate.set()
    results = await asyncio.gather(*tasks)
    assert results == [VECTOR] * 6
    assert model.max_running == 2
    assert model.calls == 6


async def test_a_caller_waits_for_a_slot_then_gets_busy() -> None:
    model = GatedModel()
    embedder = await _ready(model, concurrency=1, slot_timeout=0.05, embed_timeout=5)
    first = asyncio.create_task(embedder.embed("first"))
    async with asyncio.timeout(5):
        while model.running < 1:
            await asyncio.sleep(0.005)
    with pytest.raises(QueryEmbedderBusy):
        await embedder.embed("second")
    model.gate.set()
    assert await first == VECTOR
    assert await embedder.embed("third") == VECTOR  # the slot came back


async def test_a_timed_out_caller_does_not_free_its_slot_until_the_thread_ends() -> None:
    model = GatedModel()
    embedder = await _ready(model, concurrency=1, slot_timeout=0.05, embed_timeout=0.05)
    with pytest.raises(QueryEmbedderTimeout):
        await embedder.embed("slow")
    # The thread is still running, so the slot is still taken: a new caller must not start a second thread.
    with pytest.raises(QueryEmbedderBusy):
        await embedder.embed("next")
    assert model.max_running == 1
    model.gate.set()
    deadline = time.monotonic() + 5
    while True:
        try:
            assert await embedder.embed("after") == VECTOR
            break
        except QueryEmbedderBusy:
            assert time.monotonic() < deadline, "the slot was never released"
            await asyncio.sleep(0.01)
    assert model.max_running == 1


async def test_a_model_error_reaches_the_caller_and_frees_the_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    embedder = await _ready(FailingModel(), concurrency=1, slot_timeout=0.2, embed_timeout=5)
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="model blew up"):
        await embedder.embed("a question")
    assert "reason=RuntimeError" in caplog.text
    with pytest.raises(RuntimeError):
        await embedder.embed("again")  # not Busy: the slot was released


async def test_the_question_text_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    sentinel = "SENTINEL-salary-of-the-ceo"
    with caplog.at_level(logging.DEBUG):
        failing = await _ready(FailingModel(), concurrency=1)
        with pytest.raises(RuntimeError):
            await failing.embed(sentinel)
        model = GatedModel()
        model.gate.set()
        ok = await _ready(model, concurrency=1)
        await ok.embed(sentinel)
    assert sentinel not in caplog.text


# ---- readiness ----


async def test_the_readiness_check_follows_the_state() -> None:
    state = {"value": LOADING}
    check = make_embedder_check(lambda: state["value"])
    with pytest.raises(EmbedderNotReadyError):
        await check()
    state["value"] = FAILED
    with pytest.raises(EmbedderNotReadyError):
        await check()
    state["value"] = READY
    await check()


async def test_ready_names_the_embedder_until_it_is_loaded_and_when_the_load_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = {"value": LOADING}

    async def ok() -> None:
        return None

    checker = ReadinessChecker(
        {"database": ok, EMBEDDER: make_embedder_check(lambda: state["value"])}, cache_ttl=0.0
    )
    with caplog.at_level(logging.WARNING):
        assert await checker.failed_dependencies() == (EMBEDDER,)
        state["value"] = FAILED
        assert await checker.failed_dependencies() == (EMBEDDER,)
        state["value"] = READY
        assert await checker.failed_dependencies() == ()
    assert "embedder (loading)" in caplog.text
    assert "embedder (failed)" in caplog.text


# ---- wiring ----


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_ask_disabled_loads_no_embedder() -> None:
    settings = _settings()
    assert settings.ask_enabled is False
    assert build_query_embedder(settings) is None


def test_ask_enabled_needs_the_model_directory() -> None:
    with pytest.raises(RuntimeError, match="EMBEDDING_MODEL_DIR is not set"):
        build_query_embedder(_settings(ask_enabled=True))


def test_ask_enabled_builds_an_embedder_with_the_configured_concurrency() -> None:
    settings = _settings(ask_enabled=True, embedding_model_dir=Path("model"), embed_concurrency=3)
    embedder = build_query_embedder(settings)
    assert embedder is not None
    assert embedder.concurrency == 3
    assert embedder.state == LOADING


def test_embed_concurrency_is_bounded() -> None:
    for bad in (0, 5):
        with pytest.raises(ValueError, match="embed_concurrency"):
            _settings(embed_concurrency=bad)
    assert 1 <= _settings().embed_concurrency <= 4

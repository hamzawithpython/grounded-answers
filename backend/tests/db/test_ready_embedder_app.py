"""
The query embedder through the real app lifespan (ADR 0033, decision 7): /ready says 503 "embedder" until the model is
loaded in the background, stays 503 when the load fails, and the embedder does not exist when ASK_ENABLED is off.

The app starts the way production starts it (real database, Valkey and key file, from the `accounts` fixture). Only the
model load is replaced, by a fake that waits for the test to let it finish; the real model is measured separately
(grounded_answers.evaluation.embed_concurrency).
"""

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from grounded_answers import main as main_module
from grounded_answers.config import get_settings
from grounded_answers.main import create_app
from tests.db.auth_support import SELECTOR, Accounts

pytestmark = pytest.mark.db


class FakeModel:
    def query_embedding(self, text: str) -> list[float]:
        return [0.0]


def _ready_status(client: TestClient) -> tuple[int, Any]:
    response = client.get("/ready")
    return response.status_code, response.json()


def _wait_until_ready(client: TestClient) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _ready_status(client)[0] == 200:
            return
        time.sleep(0.3)  # /ready caches its answer for two seconds
    raise AssertionError("/ready never became 200")


@pytest.fixture
def ask_env(accounts: Accounts, monkeypatch: MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.setenv("ASK_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_MODEL_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_ready_is_503_embedder_until_the_background_load_finishes(
    ask_env: Path, monkeypatch: MonkeyPatch
) -> None:
    release = threading.Event()

    def slow_load(model_dir: Path, *, process: str = "worker") -> FakeModel:
        assert process == "web"
        assert release.wait(timeout=30)
        return FakeModel()

    monkeypatch.setattr(main_module, "load_embedder", slow_load)
    with TestClient(
        create_app(), raise_server_exceptions=False, backend_options=SELECTOR
    ) as client:
        assert (
            client.get("/health").status_code == 200
        )  # the app takes requests while the model loads
        status, body = _ready_status(client)
        assert status == 503
        assert body == {"status": "not_ready", "failed": ["embedder"]}
        release.set()
        _wait_until_ready(client)
        assert client.app.state.query_embedder.state == "ready"  # type: ignore[attr-defined]


def test_ready_stays_503_embedder_when_the_load_fails(
    ask_env: Path, monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def failing_load(model_dir: Path, *, process: str = "worker") -> FakeModel:
        raise RuntimeError(f"cannot read {model_dir}")

    monkeypatch.setattr(main_module, "load_embedder", failing_load)
    with TestClient(
        create_app(), raise_server_exceptions=False, backend_options=SELECTOR
    ) as client:
        deadline = time.monotonic() + 10
        while client.app.state.query_embedder.state != "failed":  # type: ignore[attr-defined]
            assert time.monotonic() < deadline, "the load never failed"
            time.sleep(0.05)
        time.sleep(2.1)  # let the cached readiness answer expire
        status, body = _ready_status(client)
        assert (status, body) == (503, {"status": "not_ready", "failed": ["embedder"]})
        assert client.get("/health").status_code == 200
    assert "reason=RuntimeError" in caplog.text
    assert str(ask_env) not in caplog.text  # the error message, which holds the path, is not logged


def test_ask_enabled_without_a_model_directory_stops_startup(
    accounts: Accounts, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("ASK_ENABLED", "true")
    monkeypatch.delenv("EMBEDDING_MODEL_DIR", raising=False)
    get_settings.cache_clear()
    try:
        with (
            pytest.raises(RuntimeError, match="EMBEDDING_MODEL_DIR is not set"),
            TestClient(create_app(), backend_options=SELECTOR),
        ):
            pass
    finally:
        get_settings.cache_clear()


def test_with_ask_off_there_is_no_embedder_and_ready_does_not_wait_for_one(
    accounts: Accounts, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("ASK_ENABLED", raising=False)
    get_settings.cache_clear()
    with TestClient(
        create_app(), raise_server_exceptions=False, backend_options=SELECTOR
    ) as client:
        assert client.app.state.query_embedder is None  # type: ignore[attr-defined]
        _wait_until_ready(client)
        assert _ready_status(client) == (200, {"status": "ready"})

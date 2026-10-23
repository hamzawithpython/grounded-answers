"""
Worker startup with a missing or changed model file (ADR 0022, D6): the worker stops before it connects, with
"worker not started", and the job log handler carries the embedding logger, so the startup line reaches the log.
"""

import hashlib
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from grounded_answers.embedding import model as model_module
from grounded_answers.jobs import logging_filter, worker

SMALL_PINS: dict[str, tuple[str, int]] = {"a.bin": (hashlib.sha256(b"abc").hexdigest(), 3)}
WORKER_URL = "postgresql://worker_user:not-a-real-password@127.0.0.1:5432/grounded_answers"


@pytest.fixture
def no_log_setup(monkeypatch: MonkeyPatch) -> None:
    """main() installs the job log handler. These tests do not need it, so it is replaced with a no-op."""
    monkeypatch.setattr(worker, "install_job_log_redaction", lambda: None)


def test_the_job_log_handler_carries_the_embedding_logger() -> None:
    assert "grounded_answers.embedding" in logging_filter.JOB_LOGGERS


def test_a_missing_model_file_stops_the_worker_before_it_connects(
    tmp_path: Path, monkeypatch: MonkeyPatch, no_log_setup: None
) -> None:
    monkeypatch.setenv("WORKER_DATABASE_URL", WORKER_URL)
    monkeypatch.setenv("WORKER_EMBEDDING_MODEL_DIR", str(tmp_path))  # empty: no model files at all
    with pytest.raises(
        SystemExit, match=r"worker not started: model file missing: onnx/model\.onnx"
    ):
        worker.main()


def test_a_changed_model_file_stops_the_worker(
    tmp_path: Path, monkeypatch: MonkeyPatch, no_log_setup: None
) -> None:
    monkeypatch.setattr(model_module, "PINNED_FILES", SMALL_PINS)
    (tmp_path / "a.bin").write_bytes(b"xyz")  # the same size as the pin, other bytes
    monkeypatch.setenv("WORKER_DATABASE_URL", WORKER_URL)
    monkeypatch.setenv("WORKER_EMBEDDING_MODEL_DIR", str(tmp_path))
    with pytest.raises(
        SystemExit, match=r"worker not started: model file does not match its pin: a\.bin"
    ):
        worker.main()

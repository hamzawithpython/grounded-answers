"""Production main() loads the real model from the configured directory (the embedder injection is for tests only)."""

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from grounded_answers.jobs import worker


class _Stop(Exception):
    """Raised by the stand-in loader, so the test stops after the loader is called."""


def test_main_loads_the_real_model_from_the_configured_directory(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        "WORKER_DATABASE_URL", "postgresql://worker_user:not-the-real-secret-x@127.0.0.1:5432/db"
    )
    monkeypatch.setenv("WORKER_EMBEDDING_MODEL_DIR", str(tmp_path))
    loaded: list[Path] = []

    def record_load(model_dir: Path) -> None:
        loaded.append(model_dir)
        raise _Stop

    monkeypatch.setattr(worker, "load_embedder", record_load)
    with pytest.raises(_Stop):
        worker.main()
    assert loaded == [tmp_path], (
        "main() must load the model from WORKER_EMBEDDING_MODEL_DIR, not an injected embedder"
    )

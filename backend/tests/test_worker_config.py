"""WorkerSettings reads WORKER_DATABASE_URL from the environment only (ADR 0017, decision D1)."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from grounded_answers.worker_config import WorkerSettings

WORKER_URL = "postgresql://worker_user:not-a-real-password@127.0.0.1:5432/grounded_answers"


def test_worker_settings_reads_the_url_from_the_environment(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_DATABASE_URL", WORKER_URL)
    assert str(WorkerSettings().worker_database_url) == WORKER_URL


def test_worker_settings_never_reads_a_dotenv_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A .env file in the working directory is ignored. Without the variable, startup fails."""
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)
    (tmp_path / ".env").write_text(f"WORKER_DATABASE_URL={WORKER_URL}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match="worker_database_url"):
        WorkerSettings()


def test_worker_settings_refuses_a_missing_url(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="worker_database_url"):
        WorkerSettings()

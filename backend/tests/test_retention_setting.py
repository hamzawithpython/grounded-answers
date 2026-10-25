"""DOCUMENT_RETENTION_DAYS: the default, the range, and that the web app and the worker read the same variable (ADR 0027)."""

import pytest
from pydantic import ValidationError

from grounded_answers.config import Settings
from grounded_answers.worker_config import WorkerSettings

WORKER_URL = "postgresql://worker_user:not-a-real-secret-x@127.0.0.1:5432/grounded_answers"


def test_the_default_is_thirty_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCUMENT_RETENTION_DAYS", raising=False)
    assert Settings(_env_file=None).document_retention_days == 30


@pytest.mark.parametrize("days", [1, 30, 365])
def test_the_bounds_are_accepted(monkeypatch: pytest.MonkeyPatch, days: int) -> None:
    monkeypatch.setenv("DOCUMENT_RETENTION_DAYS", str(days))
    assert Settings(_env_file=None).document_retention_days == days
    assert WorkerSettings(worker_database_url=WORKER_URL).document_retention_days == days


@pytest.mark.parametrize("days", [0, -1, 366])
def test_a_value_outside_the_bounds_stops_both_processes(
    monkeypatch: pytest.MonkeyPatch, days: int
) -> None:
    monkeypatch.setenv("DOCUMENT_RETENTION_DAYS", str(days))
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    with pytest.raises(ValidationError):
        WorkerSettings(worker_database_url=WORKER_URL)


def test_the_worker_reads_the_same_variable_as_the_web_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCUMENT_RETENTION_DAYS", "7")
    assert (
        Settings(_env_file=None).document_retention_days
        == WorkerSettings(worker_database_url=WORKER_URL).document_retention_days
        == 7
    )

"""The worker logs its retention value at startup, so a value that drifts from the web app's shows in its log (ADR 0027)."""

import logging

import pytest

from grounded_answers.jobs.worker import log_configuration


def test_the_startup_line_names_the_configured_retention(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="grounded_answers.jobs.worker"):
        log_configuration(7)
    messages = [record.getMessage() for record in caplog.records]
    assert "worker.config document_retention_days=7" in messages

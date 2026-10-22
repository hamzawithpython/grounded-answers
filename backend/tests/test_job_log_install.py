"""The job log redaction is installed on the job loggers, once, by the web app and the worker (ADR 0017)."""

import logging
from collections.abc import Iterator

import pytest

from grounded_answers.jobs.logging_filter import (
    JOB_LOGGERS,
    RedactJobArguments,
    install_job_log_redaction,
)
from grounded_answers.main import create_app


@pytest.fixture
def clean_job_loggers() -> Iterator[None]:
    """Remove every redacting handler this test adds, so no other test sees them."""
    yield
    for name in JOB_LOGGERS:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if any(isinstance(item, RedactJobArguments) for item in handler.filters):
                logger.removeHandler(handler)


def _redacting_handlers(name: str) -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger(name).handlers
        if any(isinstance(item, RedactJobArguments) for item in handler.filters)
    ]


def test_install_attaches_one_handler_to_each_job_logger(clean_job_loggers: None) -> None:
    handler = install_job_log_redaction()
    for name in JOB_LOGGERS:
        assert _redacting_handlers(name) == [handler]
        assert logging.getLogger(name).level == logging.INFO


def test_a_second_install_adds_nothing(clean_job_loggers: None) -> None:
    first = install_job_log_redaction()
    second = install_job_log_redaction()
    assert second is first
    for name in JOB_LOGGERS:
        assert len(_redacting_handlers(name)) == 1


def test_the_web_app_factory_installs_the_redaction(clean_job_loggers: None) -> None:
    create_app()
    for name in JOB_LOGGERS:
        assert len(_redacting_handlers(name)) == 1

"""The worker's log filter removes job arguments and keeps the outcome (ADR 0017). No database."""

import logging
import uuid

from grounded_answers.jobs.logging_filter import (
    REDACTED,
    RedactJobArguments,
    redact_call_string,
)

DOCUMENT_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())
CALL = f"grounded.ingest_document[42](document_id='{DOCUMENT_ID}', tenant_id='{TENANT_ID}')"


def test_call_string_keeps_the_keys_and_drops_every_value() -> None:
    assert redact_call_string(CALL) == (
        f"grounded.ingest_document[42](document_id={REDACTED}, tenant_id={REDACTED})"
    )


def test_outcome_text_after_the_call_is_kept() -> None:
    message = f"Job {CALL} ended with status: Error, lasted 0.004 s"
    redacted = redact_call_string(message)
    assert redacted.endswith("ended with status: Error, lasted 0.004 s")
    assert DOCUMENT_ID not in redacted and TENANT_ID not in redacted


def test_a_call_without_arguments_is_unchanged() -> None:
    assert redact_call_string("Job grounded.noop[7]() ended with status: Success") == (
        "Job grounded.noop[7]() ended with status: Success"
    )


def _record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("procrastinate.worker", logging.INFO, __file__, 1, message, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_filter_redacts_the_message_and_the_job_fields_and_keeps_the_record() -> None:
    job = {
        "id": 42,
        "task_name": "grounded.ingest_document",
        "status": "failed",
        "task_kwargs": {"document_id": DOCUMENT_ID, "tenant_id": TENANT_ID},
        "call_string": CALL,
    }
    record = _record(f"Job {CALL} ended with status: Error", action="job_error", job=job)

    assert RedactJobArguments().filter(record) is True

    assert record.getMessage() == (
        f"Job grounded.ingest_document[42](document_id={REDACTED}, tenant_id={REDACTED}) "
        "ended with status: Error"
    )
    assert vars(record)["job"] == {
        "id": 42,
        "task_name": "grounded.ingest_document",
        "status": "failed",
        "task_kwargs": {"document_id": REDACTED, "tenant_id": REDACTED},
        "call_string": f"grounded.ingest_document[42](document_id={REDACTED}, tenant_id={REDACTED})",
    }
    assert vars(record)["action"] == "job_error"


def test_filter_redacts_a_jobs_list_logged_when_a_job_is_enqueued() -> None:
    """The enqueue log carries the job list under the name 'jobs' (found by the worker test)."""
    job = {
        "id": 1,
        "task_name": "grounded.ingest_document",
        "task_kwargs": {"document_id": DOCUMENT_ID, "tenant_id": TENANT_ID},
        "call_string": CALL,
    }
    record = _record("Deferred 1 job", jobs=[job])

    RedactJobArguments().filter(record)

    rendered = repr(vars(record))
    assert DOCUMENT_ID not in rendered
    assert TENANT_ID not in rendered
    jobs = vars(record)["jobs"]
    assert jobs[0]["task_kwargs"] == {"document_id": REDACTED, "tenant_id": REDACTED}
    assert jobs[0]["id"] == 1


def test_filter_leaves_records_without_job_fields_alone_apart_from_the_message() -> None:
    record = _record("worker started with 4 concurrency")
    assert RedactJobArguments().filter(record) is True
    assert record.getMessage() == "worker started with 4 concurrency"

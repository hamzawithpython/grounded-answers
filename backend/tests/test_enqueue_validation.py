"""The enqueue rules, checked before anything is written (ADR 0015 decision 3, ADR 0017). No database."""

import uuid

import pytest
from pytest import MonkeyPatch

from grounded_answers.jobs import enqueue
from grounded_answers.jobs.enqueue import InvalidJobArgument, TaskNotAllowed, validate_job

KNOWN_TASK = "grounded.known_task"


@pytest.fixture
def known_task(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(enqueue, "ALLOWED_TASKS", frozenset({KNOWN_TASK}))


def test_no_task_is_allowed_by_default() -> None:
    """Step 1b-ii adds no production task, so the web app can enqueue nothing yet."""
    assert enqueue.ALLOWED_TASKS == frozenset()
    with pytest.raises(TaskNotAllowed):
        validate_job(KNOWN_TASK, {"document_id": uuid.uuid4()})


def test_an_unknown_task_name_is_refused(known_task: None) -> None:
    with pytest.raises(TaskNotAllowed):
        validate_job("grounded.something_else", {"document_id": uuid.uuid4()})


def test_a_uuid_argument_is_accepted_and_stored_as_its_string(known_task: None) -> None:
    document_id = uuid.uuid4()
    assert validate_job(KNOWN_TASK, {"document_id": document_id}) == {
        "document_id": str(document_id)
    }


@pytest.mark.parametrize(
    "value",
    [str(uuid.uuid4()), 42, b"bytes", None, ["a"], {"k": "v"}],
    ids=["str-uuid", "int", "bytes", "none", "list", "dict"],
)
def test_a_non_uuid_argument_is_refused(known_task: None, value: object) -> None:
    with pytest.raises(InvalidJobArgument):
        validate_job(KNOWN_TASK, {"document_id": value})


def test_the_refusal_names_the_type_and_never_the_value(known_task: None) -> None:
    marker = "value-that-must-not-appear-in-errors"
    with pytest.raises(InvalidJobArgument) as refused:
        validate_job(KNOWN_TASK, {"document_id": marker})
    assert marker not in str(refused.value)
    assert "str" in str(refused.value)


def test_an_argument_name_must_be_an_identifier(known_task: None) -> None:
    with pytest.raises(InvalidJobArgument):
        validate_job(KNOWN_TASK, {"document id": uuid.uuid4()})

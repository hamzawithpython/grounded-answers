"""The size rule for document bytes (SPEC §6.1, ADR 0018). No database."""

import pytest

from grounded_answers.storage.object_store import (
    MAX_OBJECT_BYTES,
    InvalidObject,
    check_object_bytes,
)


def test_the_limit_is_ten_mebibytes() -> None:
    assert MAX_OBJECT_BYTES == 10 * 1024 * 1024


def test_one_byte_and_the_limit_are_accepted() -> None:
    check_object_bytes(b"x")
    check_object_bytes(b"x" * MAX_OBJECT_BYTES)


def test_empty_bytes_are_refused() -> None:
    with pytest.raises(InvalidObject):
        check_object_bytes(b"")


def test_one_byte_over_the_limit_is_refused() -> None:
    with pytest.raises(InvalidObject):
        check_object_bytes(b"x" * (MAX_OBJECT_BYTES + 1))

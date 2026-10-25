"""The GET /documents limit and cursor, without a database (ADR 0029)."""

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from grounded_answers.documents_list import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidRequest,
    decode_cursor,
    encode_cursor,
    parse_limit,
)


def test_no_limit_means_the_default() -> None:
    assert parse_limit(None) == DEFAULT_LIMIT == 20


@pytest.mark.parametrize("raw", ["1", "20", "50"])
def test_limits_inside_the_bounds_are_accepted(raw: str) -> None:
    assert parse_limit(raw) == int(raw)


@pytest.mark.parametrize("raw", ["0", "-1", "51", "1000", "abc", "", "2.5", "1e2"])
def test_limits_outside_the_bounds_or_not_whole_numbers_are_refused(raw: str) -> None:
    with pytest.raises(InvalidRequest, match="limit must be a whole number from 1 to 50"):
        parse_limit(raw)
    assert MAX_LIMIT == 50


def test_a_cursor_round_trips() -> None:
    created = datetime(2026, 10, 25, 9, 30, 15, 123456, tzinfo=UTC)
    document = uuid.uuid4()
    assert decode_cursor(encode_cursor(created, document)) == (created, document)


@pytest.mark.parametrize(
    "raw",
    [
        "!!!",
        "not base64 at all",
        base64.urlsafe_b64encode(b"[1, 2]").decode(),
        base64.urlsafe_b64encode(b'{"c": "2026-10-25T09:30:00+00:00"}').decode(),
        base64.urlsafe_b64encode(
            b'{"c": "2026-10-25T09:30:00", "i": "' + str(uuid.uuid4()).encode() + b'"}'
        ).decode(),
        base64.urlsafe_b64encode(b'{"c": "2026-10-25T09:30:00+00:00", "i": "not-a-uuid"}').decode(),
        base64.urlsafe_b64encode(
            json.dumps({"c": "yesterday", "i": str(uuid.uuid4())}).encode()
        ).decode(),
    ],
)
def test_a_malformed_cursor_is_refused(raw: str) -> None:
    with pytest.raises(InvalidRequest, match="the cursor is not valid"):
        decode_cursor(raw)

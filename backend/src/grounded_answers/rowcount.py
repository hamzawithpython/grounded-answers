"""
Check the row count of UPDATE and DELETE statements (SECURITY R9, ADR 0005).

Row-level security filters rows silently. An UPDATE or DELETE that the policy hides affects
zero rows and raises no error (only a WITH CHECK violation raises). So every write path must
check the count it expects, and a 0 where 1 was expected is a failure, never a success.

expect_rowcount logs one line with the operation name and the two numbers. It never logs row
data, ids, emails or the statement's parameters.
"""

import logging
from typing import Any

from sqlalchemy.engine import CursorResult, Result

logger = logging.getLogger(__name__)


class RowcountMismatchError(RuntimeError):
    """A write affected a different number of rows than the code expected."""


def expect_rowcount(result: Result[Any], expected: int, operation: str) -> None:
    """
    Raise RowcountMismatchError unless the write affected exactly `expected` rows.

    operation is a fixed name chosen by the caller (for example "membership.deactivate"), so it
    never carries user data.
    """
    if not isinstance(result, CursorResult):
        raise TypeError("expect_rowcount needs the result of an UPDATE or DELETE")
    actual = result.rowcount
    if actual != expected:
        logger.error(
            "rowcount mismatch: %s affected %d rows, expected %d",
            operation,
            actual,
            expected,
        )
        raise RowcountMismatchError(f"{operation} affected {actual} rows, expected {expected}")

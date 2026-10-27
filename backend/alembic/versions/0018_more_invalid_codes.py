"""Two more invalid-output codes for answer_usage (ADR 0034, Session 5 Step 6).

Revision ID: 0018_more_invalid_codes
Revises: 0017_answer_usage_settings
Create Date: 2026-10-27

Step 6 adds two checks on the text of an answer: `answer_language` (the answer is mostly non-Latin script, so not the
English the rules ask for) and `prompt_echo` (the answer repeats a run of words from the system rules). Each can end a
request as an invalid output, and the code is stored in answer_usage.invalid_codes, whose CHECK lists the allowed codes
(migration 0017). This migration replaces that constraint with the same list plus the two new codes. The list is copied
in grounded_answers.answering.usage; a test compares the two. 0017 is applied, so it is not edited (CLAUDE.md,
migrations rule). The downgrade puts the 0017 list back; rows that already hold a new code would block it, which is
right: the data would no longer fit the old rule.
"""

from collections.abc import Sequence

from alembic import op

revision = "0018_more_invalid_codes"
down_revision = "0017_answer_usage_settings"
branch_labels = None
depends_on = None

OLD_CODES = (
    "invalid_output",
    "empty_answer",
    "no_citations",
    "bad_ref",
    "quote_ellipsis",
    "quote_too_short",
    "quote_too_long",
    "quote_not_informative",
    "quote_not_found",
    "number_not_grounded",
)
INVALID_CODES = (*OLD_CODES, "answer_language", "prompt_echo")


def _sql_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace(codes: Sequence[str]) -> Sequence[str]:
    return (
        "ALTER TABLE grounded.answer_usage DROP CONSTRAINT answer_usage_invalid_codes_check",
        "ALTER TABLE grounded.answer_usage ADD CONSTRAINT answer_usage_invalid_codes_check CHECK ("
        f"invalid_codes <@ ARRAY[{_sql_list(codes)}]::text[] AND cardinality(invalid_codes) <= 2)",
    )


def upgrade() -> None:
    for statement in _replace(INVALID_CODES):
        op.execute(statement)


def downgrade() -> None:
    for statement in _replace(OLD_CODES):
        op.execute(statement)

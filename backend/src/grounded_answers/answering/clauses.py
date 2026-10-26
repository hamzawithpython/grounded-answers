"""
The clause a quote sits in (ADR 0032, decision 3): "§4.2" instead of "§4".

A chunk stores the dotted numbers it contains, but extraction flattens sub-clauses into one line
("- 3.2.1 Probation is 3 months ... - 3.2.2 Probation for HTV and LTV drivers ..." or "rules-3.4.1 Speed limits:"),
so a label at the start of a line is the exception. A label occurrence counts when:
- its first number is the chunk's own section (a chunk in section 3 only has 3.x clauses), which drops references to
  other sections; and
- the words before it are not a cross-reference ("Table 4.1", "see 3.2", "section 3.2", "and 3.4").

A quote takes the nearest such label at or before its start. A quote before the first label of its chunk (the chunk
begins in the middle of a clause) takes the section instead; the previous chunk's last label is not looked up in v1.
A table chunk is always section-level (the known limit, q012): its text has no clause structure.

On the 45 expected (document, clause) pairs of the demo set every clause is found this way
(docs/EVAL.md, Step 4). That shows the labels can be found, not that each quote gets the right one.
"""

import re
from dataclasses import dataclass
from typing import Final

from grounded_answers.ingest.chunking import DOTTED_LABEL

TABLE_PREFIX: Final = "[table]"
_CROSS_REFERENCE: Final = re.compile(
    r"\b(?:table|figure|see|section|sections|clause|clauses|under|in|per|and|to|of|with|ref)\W*$",
    re.IGNORECASE,
)
_LOOKBEHIND: Final = 12


@dataclass(frozen=True)
class ClauseLabel:
    # "clause" (4.2), "section" (4), "table" (4, a table chunk) or "none" (the chunk has no section number).
    level: str
    label: str | None


def is_table_chunk(body: str) -> bool:
    return body.startswith(TABLE_PREFIX)


def clause_candidates(body: str, section_number: str | None) -> list[tuple[int, str]]:
    """(offset, label) of the clause labels in a text chunk, in order."""
    if section_number is None or is_table_chunk(body):
        return []
    section = section_number.split(".")[0]
    found: list[tuple[int, str]] = []
    for match in DOTTED_LABEL.finditer(body):
        label = match.group(1)
        if label.split(".")[0] != section:
            continue
        if _CROSS_REFERENCE.search(body[max(0, match.start() - _LOOKBEHIND) : match.start()]):
            continue
        found.append((match.start(), label))
    return found


def clause_at(body: str, section_number: str | None, offset: int) -> ClauseLabel:
    """The label in force at `offset` (a position in the stored body)."""
    if is_table_chunk(body):
        return ClauseLabel("table", section_number)
    if section_number is None:
        return ClauseLabel("none", None)
    label = None
    for start, candidate in clause_candidates(body, section_number):
        if start > offset:
            break
        label = candidate
    if label is None:
        return ClauseLabel("section", section_number)
    return ClauseLabel("clause", label)

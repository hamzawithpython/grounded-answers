"""
The one structure every format's extraction returns, for chunking in Step 5 (ADR 0020, decision 6).

Level rule (ADR 0020, D-B): a numbered heading's level is the number of its parts, so "4" is level 1,
"4.2" is level 2 and "4.2.3" is level 3. An unnumbered heading uses the level its format gives it
(a heading style, a heading tag, or a font size). The level is always between 1 and 6.
"""

import re
from dataclasses import dataclass, field
from typing import Final

MAX_LEVEL: Final = 6
_NUMBER: Final = re.compile(r"^\d+(?:\.\d+)*$")
_NUMBERED_TITLE: Final = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")


@dataclass(frozen=True)
class Table:
    """A table as rows of cell text. Used for DOCX and Markdown; PDF tables are text (D-A)."""

    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Section:
    number: str | None
    heading: str
    level: int
    page: int | None
    text: str
    tables: tuple[Table, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not 1 <= self.level <= MAX_LEVEL:
            raise ValueError(f"section level must be 1 to {MAX_LEVEL}")
        if self.number is not None and not _NUMBER.fullmatch(self.number):
            raise ValueError("section number must look like 4 or 4.2.3")
        if self.page is not None and self.page < 1:
            raise ValueError("page numbers start at 1")


@dataclass(frozen=True)
class Extracted:
    format: str
    page_count: int | None
    sections: tuple[Section, ...]

    def has_text(self) -> bool:
        return any(section.text.strip() or section.heading.strip() for section in self.sections)


def split_number(heading: str) -> tuple[str | None, str]:
    """Split "4.2.3 Vehicle checks" into ("4.2.3", "Vehicle checks"). No number: (None, heading)."""
    match = _NUMBERED_TITLE.match(heading.strip())
    if match is None:
        return None, heading.strip()
    return match.group(1), match.group(2).strip()


def level_from_number(number: str) -> int:
    """The number of parts: "4" is 1, "4.2.3" is 3. Capped at MAX_LEVEL."""
    return min(number.count(".") + 1, MAX_LEVEL)

"""
PDF into the common structure (ADR 0020, decisions D-A, D-B and D-C).

- Tables are text (D-A): pypdf returns cell text, and rows are kept as lines.
- A heading line is a short line in a font larger than the body size (D-B). The body size is the
  font size that covers the most characters in the document.
- A heading's level comes from its numbering when it has one ("4.2.3" is level 3). An unnumbered
  heading (such as the title) takes its level from its font size: the largest heading size is level 1.
- A PDF with no text layer (a scan) yields no sections, and the caller refuses it as "no text found"
  (D-C). OCR is out of scope for v1.
"""

import io
import re
from collections import Counter
from dataclasses import dataclass
from typing import Final

from pypdf import PdfReader

from grounded_answers.extract.model import (
    MAX_LEVEL,
    Extracted,
    Section,
    level_from_number,
    split_number,
)

HEADING_MIN_EXTRA_POINTS: Final = 0.5
HEADING_MAX_CHARS: Final = 120
_SPACES: Final = re.compile(r"\s+")


@dataclass
class _Line:
    y: float
    size: float
    text: str
    page: int


def _page_runs(page: object) -> list[tuple[float, float, str]]:
    """Each text run with its vertical position and font size, in drawing order."""
    runs: list[tuple[float, float, str]] = []

    def visitor(
        text: str, cm: list[float], tm: list[float], font_dict: object, font_size: float
    ) -> None:
        if text.strip():
            runs.append((round(float(cm[5]) + float(tm[5]), 1), round(float(font_size), 1), text))

    page.extract_text(visitor_text=visitor)  # type: ignore[attr-defined]
    return runs


def _lines(runs: list[tuple[float, float, str]], page: int) -> list[_Line]:
    """Join consecutive runs that share a vertical position into one line."""
    lines: list[_Line] = []
    for y, size, text in runs:
        if lines and lines[-1].y == y:
            lines[-1].text += text
        else:
            lines.append(_Line(y=y, size=size, text=text, page=page))
    for line in lines:
        line.text = _SPACES.sub(" ", line.text).strip()
    return [line for line in lines if line.text]


def _body_size(lines: list[_Line]) -> float:
    """
    The font size that covers the most characters (weighted by length, not by line count).

    A tie goes to the smaller size: body text is smaller than the headings above it. Without this,
    the tie would be broken by insertion order, and a title could be taken for body text.
    """
    weight: Counter[float] = Counter()
    for line in lines:
        weight[line.size] += len(line.text)
    best = max(weight.items(), key=lambda item: (item[1], -item[0]))
    return best[0]


def _is_heading(line: _Line, body: float) -> bool:
    return line.size >= body + HEADING_MIN_EXTRA_POINTS and len(line.text) <= HEADING_MAX_CHARS


def _level_by_size(size: float, heading_sizes: list[float]) -> int:
    """The largest heading size is level 1, the next is level 2, and so on (capped)."""
    return min(heading_sizes.index(size) + 1, MAX_LEVEL)


def extract_pdf(data: bytes) -> Extracted:
    reader = PdfReader(io.BytesIO(data))
    all_lines: list[_Line] = []
    for page_number, page in enumerate(reader.pages, start=1):
        all_lines.extend(_lines(_page_runs(page), page_number))
    if not all_lines:
        return Extracted(format="pdf", page_count=len(reader.pages), sections=())

    body = _body_size(all_lines)
    heading_sizes = sorted(
        {line.size for line in all_lines if _is_heading(line, body)}, reverse=True
    )

    sections: list[tuple[str | None, str, int, int | None, list[str]]] = []
    current: tuple[str | None, str, int, int | None, list[str]] = (
        None,
        "",
        1,
        all_lines[0].page,
        [],
    )
    for line in all_lines:
        if _is_heading(line, body):
            sections.append(current)
            number, title = split_number(line.text)
            level = (
                level_from_number(number) if number else _level_by_size(line.size, heading_sizes)
            )
            current = (number, title, level, line.page, [])
        else:
            current[4].append(line.text)
    sections.append(current)

    built = tuple(
        Section(number=number, heading=heading, level=level, page=page, text="\n".join(text))
        for number, heading, level, page, text in sections
        if heading or text
    )
    return Extracted(format="pdf", page_count=len(reader.pages), sections=built)

"""
Markdown and plain text into the common structure (ADR 0020).

Markdown: headings (with their level from the numbering, or from the heading tag when unnumbered),
paragraphs, list items and code blocks as section text, and tables as rows.
Plain text: one section with no heading. Its text is the whole file.
"""

from markdown_it import MarkdownIt
from markdown_it.token import Token

from grounded_answers.extract.model import (
    Extracted,
    Section,
    Table,
    level_from_number,
    split_number,
)


class _Builder:
    """Collects one section while the token stream is read. Frozen into a Section at the end."""

    def __init__(self, number: str | None, heading: str, level: int) -> None:
        self.number = number
        self.heading = heading
        self.level = level
        self.text: list[str] = []
        self.tables: list[Table] = []

    def build(self) -> Section:
        return Section(
            number=self.number,
            heading=self.heading,
            level=self.level,
            page=None,
            text="\n\n".join(part for part in self.text if part.strip()),
            tables=tuple(self.tables),
        )


def _table_rows(tokens: list[Token], start: int) -> tuple[Table, int]:
    """Read one table starting at table_open. Returns the table and the index after table_close."""
    rows: list[tuple[str, ...]] = []
    current: list[str] = []
    index = start
    while index < len(tokens) and tokens[index].type != "table_close":
        token = tokens[index]
        if token.type == "tr_open":
            current = []
        elif token.type == "tr_close":
            rows.append(tuple(current))
        elif token.type == "inline":
            current.append(token.content.strip())
        index += 1
    return Table(rows=tuple(rows)), index + 1


def extract_markdown(source: str) -> Extracted:
    tokens = MarkdownIt("commonmark").enable("table").parse(source)
    sections: list[_Builder] = []
    current = _Builder(None, "", 1)  # text before the first heading
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == "heading_open":
            heading_text = tokens[index + 1].content.strip()
            number, title = split_number(heading_text)
            level = level_from_number(number) if number else int(token.tag[1])
            sections.append(current)
            current = _Builder(number, title, level)
            index += 3  # heading_open, inline, heading_close
            continue
        if token.type == "table_open":
            table, index = _table_rows(tokens, index)
            current.tables.append(table)
            continue
        if token.type == "inline" and tokens[index - 1].type != "heading_open":
            current.text.append(token.content.strip())
        elif token.type in ("fence", "code_block"):
            current.text.append(token.content.rstrip("\n"))
        index += 1
    sections.append(current)
    built = tuple(
        section.build() for section in sections if section.heading or section.text or section.tables
    )
    return Extracted(format="md", page_count=None, sections=built)


def extract_text_file(source: str) -> Extracted:
    """Plain text has no structure: one section with no number and no heading."""
    section = Section(number=None, heading="", level=1, page=None, text=source.strip())
    return Extracted(format="txt", page_count=None, sections=(section,))

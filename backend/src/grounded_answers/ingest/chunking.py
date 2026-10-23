"""
Split extracted sections into chunks: strategy S1 of ADR 0021 (section-aware, about 1,200 characters, tables whole).

- A numbered section is split at line breaks into pieces of at most MAX_CHARS. A single line longer than that is
  cut into pieces too, so no chunk exceeds the limit.
- Each table is one chunk, never split.
- Each chunk records the section number and the clause labels it contains (for citations such as 3.2.2).

The embedding input adds a prefix with the document title and version. That prefix is not stored in body.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from grounded_answers.extract.model import Extracted, Section, Table

MAX_CHARS = 1200
# The chunks table limits a body to 20000 characters (migration 0008). Longer text is refused, not truncated.
MAX_BODY_CHARS = 20000

# A clause label has at least one dot: 3.2, 3.2.2. Top-level numbers are only labels when they are a section.
_DOTTED = re.compile(r"(?<![\d.])(\d+(?:\.\d+)+)(?![\d])")


class ChunkTooLong(ValueError):
    """A table or a chunk exceeds the stored body limit. The version is refused with the 'unreadable' reason."""


@dataclass(frozen=True)
class Chunk:
    position: int
    section_number: str | None
    heading: str
    clause_labels: tuple[str, ...]
    body: str


def clause_labels(body: str, section_number: str | None) -> tuple[str, ...]:
    labels = set(_DOTTED.findall(body))
    if section_number:
        labels.add(section_number)
    return tuple(sorted(labels))


def _pieces(text: str, max_chars: int) -> Iterator[str]:
    current = ""
    for line in (ln for ln in text.split("\n") if ln.strip()):
        for part in (line[i : i + max_chars] for i in range(0, len(line), max_chars)):
            if current and len(current) + len(part) + 1 > max_chars:
                yield current
                current = part
            else:
                current = f"{current}\n{part}" if current else part
    if current:
        yield current


def _table_text(table: Table) -> str:
    return "\n".join(" | ".join(cell.strip() for cell in row) for row in table.rows)


def _section_bodies(section: Section, max_chars: int) -> list[str]:
    return list(_pieces(section.text, max_chars))


def section_chunks(extracted: Extracted, max_chars: int = MAX_CHARS) -> list[Chunk]:
    """The chunks of one extracted document, in document order. Empty bodies are dropped."""
    chunks: list[Chunk] = []

    def add(section: Section, body: str) -> None:
        if not body.strip():
            return
        if len(body) > MAX_BODY_CHARS:
            raise ChunkTooLong("a chunk is longer than the stored limit")
        chunks.append(
            Chunk(
                position=len(chunks),
                section_number=section.number,
                heading=section.heading,
                clause_labels=clause_labels(body, section.number),
                body=body,
            )
        )

    for section in extracted.sections:
        for body in _section_bodies(section, max_chars):
            add(section, body)
        for table in section.tables:
            add(section, f"[table]\n{_table_text(table)}")
    return chunks


def embedding_text(chunk: Chunk, title: str, version_no: int) -> str:
    """The text the model embeds: a prefix that names the document, version, section and heading, then the body.
    The 'passage: ' prefix is added by the embedder, not here."""
    label = " ".join(part for part in (chunk.section_number or "", chunk.heading) if part)
    return f"{title} v{version_no} | {label}\n{chunk.body}".strip()


def embedding_texts(chunks: Sequence[Chunk], title: str, version_no: int) -> list[str]:
    return [embedding_text(chunk, title, version_no) for chunk in chunks]

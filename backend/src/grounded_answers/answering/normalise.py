"""
Text normalisation for quote checking (ADR 0032, decision 2), with a map back to the original offsets.

The same function runs on the quote and on the chunk body, so the two are compared in one form:
- invisible characters (text.py: tag characters, bidirectional controls, U+200B, U+FEFF) removed; the joiners U+200C and
  U+200D stay;
- Unicode NFKC, then case-folding;
- curly quotes become straight quotes; en dashes, em dashes and minus signs become "-";
- every run of whitespace (newlines and non-breaking spaces included), the table separator "|" and the bullet
  characters become one space;
- a lone "-" between spaces (a list marker the extractor joined into the line, as in "... - 3.2.2 Probation ...") is dropped.

`normalise` returns the normalised text and, for each of its characters, the offset in the original text of the
character it came from, so a match in the normalised body maps back to a position in the stored body (clauses.py).

The character sets are written as code points, so that no look-alike character sits in the source.
"""

import unicodedata
from typing import Final

from grounded_answers.answering.text import is_invisible

# Single curly quotes (U+2018 to U+201B) become "'", double ones (U+201C to U+201F) become '"'.
_QUOTES: Final = {
    **{chr(code): "'" for code in range(0x2018, 0x201C)},
    **{chr(code): '"' for code in range(0x201C, 0x2020)},
}
# Hyphens and dashes (U+2010 to U+2015) and the minus sign (U+2212).
_DASHES: Final = frozenset(chr(code) for code in (*range(0x2010, 0x2016), 0x2212))
# "|" and bullets: bullet, black circle, small black square, middle dot, asterisk.
_SPACE_LIKE: Final = frozenset(
    ["|", *(chr(code) for code in (0x2022, 0x25CF, 0x25AA, 0x00B7, 0x2A))]
)


def _chars(text: str) -> list[tuple[str, int]]:
    """Each original character, expanded by NFKC and case-folding, with its original offset."""
    out: list[tuple[str, int]] = []
    for offset, original in enumerate(text):
        if is_invisible(original):
            continue  # removed on both sides of the comparison (text.py), so a quote still matches
        for char in unicodedata.normalize("NFKC", original).casefold():
            if char in _QUOTES:
                char = _QUOTES[char]
            elif char in _DASHES:
                char = "-"
            if char.isspace() or char in _SPACE_LIKE:
                char = " "
            out.append((char, offset))
    return out


def normalise(text: str) -> tuple[str, list[int]]:
    collapsed: list[tuple[str, int]] = []
    for char, offset in _chars(text):
        if char == " " and (not collapsed or collapsed[-1][0] == " "):
            continue
        collapsed.append((char, offset))
    # Drop a lone "-" token (the extractor's joined list marker), then collapse and trim.
    kept: list[tuple[str, int]] = []
    for i, (char, offset) in enumerate(collapsed):
        lone = (
            char == "-"
            and (i == 0 or collapsed[i - 1][0] == " ")
            and (i + 1 == len(collapsed) or collapsed[i + 1][0] == " ")
        )
        if lone or (char == " " and kept and kept[-1][0] == " "):
            continue
        kept.append((char, offset))
    while kept and kept[0][0] == " ":
        kept.pop(0)
    while kept and kept[-1][0] == " ":
        kept.pop()
    return "".join(c for c, _ in kept), [o for _, o in kept]

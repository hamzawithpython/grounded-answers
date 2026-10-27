"""
Characters a person cannot see, and the script of a piece of text (ADR 0034, Step 6).

Invisible characters let a document carry an instruction that no admin can read but a model can: Unicode "tag"
characters (U+E0000 to U+E007F) spell out ASCII text invisibly ("ASCII smuggling"), bidirectional controls reorder what
is displayed, and zero-width characters hide or split words. They are removed from everything the model sees (the
sources and the question), and from both sides of the quote check, so a quote still matches a passage that held one.

What is removed: the tag characters, the bidirectional controls (U+061C, U+200E, U+200F, U+202A to U+202E, U+2066 to
U+2069), the zero-width space U+200B and the byte-order mark U+FEFF.

What is kept on purpose: the zero-width non-joiner U+200C and the zero-width joiner U+200D. They change how letters join
in Urdu, Persian and Indic scripts, so removing them would change the words of v2 Urdu documents. Considered and not
removed: the soft hyphen U+00AD and the word joiner U+2060 (rare, and legitimate in typesetting).
"""

import unicodedata
from typing import Final

_REMOVED: Final = frozenset(
    [
        *range(0xE0000, 0xE0080),  # tag characters
        0x061C,  # arabic letter mark
        0x200B,  # zero-width space
        0x200E,  # left-to-right mark
        0x200F,  # right-to-left mark
        *range(0x202A, 0x202F),  # embeddings and overrides
        *range(0x2066, 0x206A),  # isolates
        0xFEFF,  # byte-order mark, wherever it sits
    ]
)
_TABLE: Final = dict.fromkeys(_REMOVED)

# Answer text may be this share non-Latin before it is refused as "not English" (see non_latin_letters).
NON_LATIN_SHARE: Final = 0.2
NON_LATIN_MINIMUM: Final = 3


def is_invisible(char: str) -> bool:
    return ord(char) in _REMOVED


def strip_invisible(text: str) -> str:
    """`text` without the characters listed in the module docstring."""
    return text.translate(_TABLE)


def non_latin_letters(text: str) -> tuple[int, int]:
    """(letters that are not Latin script, all letters). A letter is Latin when its Unicode name says so."""
    letters = other = 0
    for char in text:
        if char.isalpha():
            letters += 1
            if not unicodedata.name(char, "").startswith("LATIN"):
                other += 1
    return other, letters


def is_mostly_non_latin(text: str) -> bool:
    """True when the text is clearly not written in Latin script: at least 3 such letters and over 20 % of the
    letters. A name in another script inside an English sentence passes; a reply in Urdu script does not. Roman Urdu,
    French and other Latin-script languages pass: only a language detector would tell them from English (ADR 0034)."""
    other, letters = non_latin_letters(text)
    return other >= NON_LATIN_MINIMUM and other / letters > NON_LATIN_SHARE

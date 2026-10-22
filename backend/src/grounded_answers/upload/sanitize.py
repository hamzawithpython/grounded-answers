"""
Sanitize the client's filename for display (SPEC §6.1, ADR 0019, decision D4).

The filename is never used as a storage key and never used to choose a type: it is display text,
so it must not carry anything that changes how it is shown or where it points.

Rules, in order:
1. keep only the last path segment, splitting on both '/' and '\\' (path traversal);
2. remove control characters, NUL, bidirectional controls (for example U+202E, which reverses
   the display of the rest of the name) and zero-width characters (U+200B, U+FEFF, ...);
3. normalise to Unicode NFC, so the same name always has the same bytes;
4. collapse runs of whitespace and trim;
5. limit to 255 characters (the database limit), and use "document" when nothing is left.
"""

import re
import unicodedata

MAX_FILENAME_CHARS = 255
FALLBACK_NAME = "document"

# Unicode categories removed outright: Cc (control, includes NUL), Cf (format: bidi overrides,
# zero-width joiners and spaces, byte-order mark), Co and Cs (private use and surrogates).
_REMOVED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs"})
_SEPARATORS = re.compile(r"[\\/]")
# Tabs and line breaks are separators in a name, so they become spaces before control characters
# are removed. Removing them outright would join the words either side.
_LINE_BREAKS_TO_SPACE = str.maketrans({"\t": " ", "\n": " ", "\r": " ", "\x0b": " ", "\x0c": " "})
_WHITESPACE = re.compile(r"\s+")


def sanitize_filename(raw: str) -> str:
    """Return a display-safe name. Never raises; never returns an empty string."""
    last_segment = _SEPARATORS.split(raw)[-1].translate(_LINE_BREAKS_TO_SPACE)
    kept = "".join(
        char for char in last_segment if unicodedata.category(char) not in _REMOVED_CATEGORIES
    )
    normalised = unicodedata.normalize("NFC", kept)
    collapsed = _WHITESPACE.sub(" ", normalised).strip()
    limited = collapsed[:MAX_FILENAME_CHARS].strip()
    if limited in ("", ".", ".."):
        return FALLBACK_NAME
    return limited

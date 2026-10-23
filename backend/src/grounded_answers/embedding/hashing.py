"""SHA-256 of a file, read in blocks. One implementation for the fetch step and the startup check."""

import hashlib
from pathlib import Path

_BLOCK = 1 << 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()

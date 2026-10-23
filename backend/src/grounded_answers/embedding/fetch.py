"""
Download the pinned model files once, at build or deploy time (ADR 0022, decision D7).

Production never downloads at startup. Run this step while building the image, or before the first start:

    uv run python -m grounded_answers.embedding.fetch <cache directory>

Every downloaded file is hashed before it is kept. A file that is already in the cache is never replaced:
it is checked, and a mismatch raises ModelIntegrityError, so a corrupted or tampered cache fails loudly.
"""

import os
import shutil
import sys
import urllib.request
from pathlib import Path

from grounded_answers.embedding.hashing import sha256_file
from grounded_answers.embedding.model import ModelIntegrityError, verify_model_dir
from grounded_answers.embedding.pinned import MODEL_NAME, MODEL_REVISION, PINNED_FILES

DOWNLOAD_TIMEOUT_SECONDS = 120


def model_dir(cache_root: Path) -> Path:
    """The directory for the pinned revision. A new revision gets a new directory."""
    return cache_root / MODEL_REVISION


def fetch_pinned_model(cache_root: Path) -> Path:
    """Download any missing file, verify every file, and return the model directory."""
    directory = model_dir(cache_root)
    for relative, (expected_sha, expected_size) in PINNED_FILES.items():
        target = directory / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        url = f"https://huggingface.co/{MODEL_NAME}/resolve/{MODEL_REVISION}/{relative}"
        # The URL is built from pinned constants only, never from input. The file is hashed before it is kept.
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            with partial.open("wb") as out:
                shutil.copyfileobj(response, out)
        if partial.stat().st_size != expected_size or sha256_file(partial) != expected_sha:
            partial.unlink()
            raise ModelIntegrityError(f"downloaded file does not match its pin: {relative}")
        os.replace(partial, target)
    verify_model_dir(directory)
    return directory


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: python -m grounded_answers.embedding.fetch <cache directory>", file=sys.stderr
        )
        return 2
    try:
        directory = fetch_pinned_model(Path(argv[1]))
    except ModelIntegrityError as exc:
        print(f"model fetch failed: {exc}", file=sys.stderr)
        return 1
    print(f"model ready: {directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

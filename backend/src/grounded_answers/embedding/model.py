"""
Load the pinned embedding model from a local directory, offline (ADR 0021, ADR 0022 decision D6 and D7).

The model is loaded once per worker process, at startup. Startup fails with a clear message if any pinned
file is missing or does not match its hash. Nothing is downloaded here: fetch.py does that at build time.
Input text is never logged.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from fastembed import TextEmbedding
from fastembed.common.model_description import ModelSource, PoolingType

from grounded_answers.embedding.hashing import sha256_file
from grounded_answers.embedding.pinned import (
    BATCH_SIZE,
    MODEL_FILE,
    MODEL_NAME,
    PASSAGE_PREFIX,
    PINNED_FILES,
    QUERY_PREFIX,
)
from grounded_answers.models import EMBEDDING_DIMENSIONS

# fastembed's size estimate for the registry entry. It is informational only.
_SIZE_IN_GB: float = 0.47


class ModelIntegrityError(RuntimeError):
    """A pinned model file is missing, or does not match its pinned hash or size."""


class Embedder(Protocol):
    """Turns text into vectors. The worker uses passage embeddings; the query path will use query_embedding."""

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...


def verify_model_dir(directory: Path) -> None:
    """Refuse a directory whose pinned files are missing or differ from their pins. Reads every file once."""
    for relative, (expected_sha, expected_size) in PINNED_FILES.items():
        path = directory / relative
        if not path.is_file():
            raise ModelIntegrityError(f"model file missing: {relative}")
        if path.stat().st_size != expected_size or sha256_file(path) != expected_sha:
            raise ModelIntegrityError(f"model file does not match its pin: {relative}")


def _register_model() -> None:
    registered: list[dict[str, Any]] = TextEmbedding.list_supported_models()
    if any(entry["model"] == MODEL_NAME for entry in registered):
        return
    TextEmbedding.add_custom_model(
        model=MODEL_NAME,
        pooling=PoolingType.MEAN,
        normalization=True,
        sources=ModelSource(hf=MODEL_NAME),
        dim=EMBEDDING_DIMENSIONS,
        model_file=MODEL_FILE,
        size_in_gb=_SIZE_IN_GB,
    )


class FastEmbedder:
    """multilingual-e5-small through fastembed, from a verified local directory (offline)."""

    def __init__(self, model_dir: Path, *, threads: int | None = None) -> None:
        verify_model_dir(model_dir)
        _register_model()
        self._model = TextEmbedding(
            model_name=MODEL_NAME, specific_model_path=str(model_dir), threads=threads
        )

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        prefixed = [PASSAGE_PREFIX + text for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(prefixed), BATCH_SIZE):
            batch = prefixed[start : start + BATCH_SIZE]
            vectors.extend(
                vector.tolist() for vector in self._model.embed(batch, batch_size=BATCH_SIZE)
            )
        return vectors

    def query_embedding(self, text: str) -> list[float]:
        """Used by Session 5's query path. Same model, the query prefix."""
        vector = next(iter(self._model.embed([QUERY_PREFIX + text], batch_size=1)))
        values: list[float] = vector.tolist()
        return values

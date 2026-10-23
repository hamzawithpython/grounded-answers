"""
Embed text with the pinned model, from a verified local directory, offline (ADR 0021, ADR 0022 D6 and D7).

Runtime: onnxruntime runs the model, and tokenizers splits the text. Nothing else is involved. fastembed was
replaced in this step because it caps pillow below 13.0, and pillow 12.x carries 35 advisories that the 13.0.0
release fixes (ADR 0022, amendment of 2026-10-23). We never process images.

The steps match the e5 model card: "passage: " or "query: " prefix, truncation at the model's maximum length,
attention-masked mean pooling over the last hidden state, then L2 normalisation.

The model is loaded once per worker process, at startup. Startup fails with a clear message if any pinned file
is missing or does not match its hash. Nothing is downloaded here: fetch.py does that at build time.
Input text is never logged.
"""

import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol

import numpy as np
import onnxruntime as ort  # type: ignore[import-untyped]  # onnxruntime ships no stubs (ADR 0022)
from tokenizers import Tokenizer

from grounded_answers.embedding.hashing import sha256_file
from grounded_answers.embedding.pinned import (
    BATCH_SIZE,
    MODEL_FILE,
    MODEL_NAME,
    MODEL_REVISION,
    PASSAGE_PREFIX,
    PINNED_FILES,
    QUERY_PREFIX,
)

# The model's maximum input length in tokens (tokenizer_config.json, model_max_length). Longer text is truncated.
MAX_TOKENS: Final = 512
# ONNX Runtime threads for one inference call. The value is set from the measurement in docs/EVAL.md.
INTRA_OP_THREADS: Final = 2
_PAD_SYMBOL: Final = "<pad>"


logger = logging.getLogger(__name__)


class ModelIntegrityError(RuntimeError):
    """A pinned model file is missing, or does not match its pinned hash or size."""


class Embedder(Protocol):
    """Turns text into vectors. The worker uses passage embeddings; the query path uses query_embedding."""

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...


def verify_model_dir(directory: Path) -> None:
    """Refuse a directory whose pinned files are missing or differ from their pins. Reads every file once."""
    for relative, (expected_sha, expected_size) in PINNED_FILES.items():
        path = directory / relative
        if not path.is_file():
            raise ModelIntegrityError(f"model file missing: {relative}")
        if path.stat().st_size != expected_size or sha256_file(path) != expected_sha:
            raise ModelIntegrityError(f"model file does not match its pin: {relative}")


def _session_options() -> ort.SessionOptions:
    options = ort.SessionOptions()
    options.intra_op_num_threads = INTRA_OP_THREADS
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    # Inputs vary in length, so a fixed memory pattern cannot be reused. The arena is disabled to keep
    # the resident memory low on a small host; the measurement is in docs/EVAL.md.
    options.enable_mem_pattern = False
    options.enable_cpu_mem_arena = False
    return options


class PinnedEmbedder:
    """multilingual-e5-small through onnxruntime and tokenizers, from a verified local directory (offline)."""

    def __init__(self, model_dir: Path) -> None:
        verify_model_dir(model_dir)
        tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=MAX_TOKENS)
        pad_id = tokenizer.token_to_id(_PAD_SYMBOL)
        if pad_id is None:
            raise ModelIntegrityError(f"the tokenizer has no {_PAD_SYMBOL} token")
        tokenizer.enable_padding(pad_id=pad_id, pad_token=_PAD_SYMBOL)
        self._tokenizer = tokenizer
        self._session = ort.InferenceSession(
            str(model_dir / MODEL_FILE),
            sess_options=_session_options(),
            providers=["CPUExecutionProvider"],
        )

    def _embed_batch(self, texts: Sequence[str]) -> np.ndarray:
        encodings = self._tokenizer.encode_batch(list(texts))
        input_ids = np.array([encoding.ids for encoding in encodings], dtype=np.int64)
        attention_mask = np.array(
            [encoding.attention_mask for encoding in encodings], dtype=np.int64
        )
        token_type_ids = np.zeros_like(input_ids)
        hidden: np.ndarray = self._session.run(
            ["last_hidden_state"],
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )[0]
        mask = attention_mask[:, :, np.newaxis].astype(np.float32)
        pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        normalised: np.ndarray = (pooled / norms).astype(np.float32)
        return normalised

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            vectors.extend(self._embed_batch(texts[start : start + BATCH_SIZE]).tolist())
        return vectors

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([PASSAGE_PREFIX + text for text in texts])

    def query_embedding(self, text: str) -> list[float]:
        """Used by Session 5's query path. Same model, the query prefix."""
        return self._embed([QUERY_PREFIX + text])[0]


def load_embedder(model_dir: Path) -> PinnedEmbedder:
    """Verify and load the model, then log one line. Called once at worker startup (ADR 0022, D6).

    The line names the model and its pinned revision, and the load time. It holds no file path and no document data.
    """
    started = time.monotonic()
    embedder = PinnedEmbedder(model_dir)
    load_ms = round((time.monotonic() - started) * 1000)
    logger.info(
        "worker.model_loaded model=%s revision=%s load_ms=%d", MODEL_NAME, MODEL_REVISION, load_ms
    )
    return embedder

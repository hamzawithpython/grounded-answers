"""
The real pinned model, loaded offline (ADR 0021, ADR 0022 decision D7). One test, in the DB job, because CI restores
the model from its cache there. It never skips: a missing model directory is a failure (ADR 0006).

Set TEST_EMBEDDING_MODEL_DIR to the revision directory that `python -m grounded_answers.embedding.fetch` prints.
"""

import math
import os
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from grounded_answers.embedding.model import PinnedEmbedder
from grounded_answers.models import EMBEDDING_DIMENSIONS

pytestmark = pytest.mark.db


def _model_dir() -> Path:
    raw = os.environ.get("TEST_EMBEDDING_MODEL_DIR")
    if not raw:
        raise RuntimeError(
            "TEST_EMBEDDING_MODEL_DIR is not set. Run `python -m grounded_answers.embedding.fetch <cache>` "
            "and set it to the printed directory (docs/RUNBOOK.md, 'Embedding model files'). "
            "This test never skips (ADR 0006)."
        )
    return Path(raw)


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / (math.hypot(*left) * math.hypot(*right))


def test_the_pinned_model_gives_normalised_vectors_of_the_stored_size_offline(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "HF_HUB_OFFLINE", "1"
    )  # any attempt to download fails here, not in production
    embedder = PinnedEmbedder(_model_dir())
    vectors = embedder.embed_passages(
        ["Probation is six months.", "Overtime is paid at two times the rate."]
    )
    assert len(vectors) == 2
    for vector in vectors:
        assert len(vector) == EMBEDDING_DIMENSIONS
        assert abs(math.hypot(*vector) - 1.0) < 1e-3


def test_the_model_ranks_the_relevant_passage_above_an_unrelated_one(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    embedder = PinnedEmbedder(_model_dir())
    query = embedder.query_embedding("How long is the probation period?")
    relevant, unrelated = embedder.embed_passages(
        ["Probation is six months for drivers.", "Overtime is paid at two times the hourly rate."]
    )
    assert _cosine(query, relevant) > _cosine(query, unrelated)

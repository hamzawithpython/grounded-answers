"""retrieve() refuses an unusable question, vector or count before it touches the database (ADR 0030)."""

import math
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from grounded_answers.config import Settings
from grounded_answers.models import EMBEDDING_DIMENSIONS
from grounded_answers.retrieval import RetrievalInputError, retrieve
from grounded_answers.retrieval.search import MAX_QUESTION_CHARS

pytestmark = pytest.mark.anyio

GOOD = [0.1] * EMBEDDING_DIMENSIONS
# retrieve() validates first, so a session that is never used proves that nothing was sent.
NO_SESSION = cast(AsyncSession, object())


@pytest.mark.parametrize(
    ("question", "vector", "top_k"),
    [
        ("", GOOD, 5),
        ("   \n", GOOD, 5),
        ("x" * (MAX_QUESTION_CHARS + 1), GOOD, 5),
        ("how long", GOOD[:-1], 5),
        ("how long", [*GOOD[:-1], math.nan], 5),
        ("how long", [*GOOD[:-1], math.inf], 5),
        ("how long", GOOD, 0),
        ("how long", GOOD, 51),
    ],
)
async def test_unusable_input_is_refused_before_the_database(
    question: str, vector: list[float], top_k: int
) -> None:
    with pytest.raises(RetrievalInputError):
        await retrieve(NO_SESSION, question, vector, top_k=top_k)


def test_the_top_k_setting_defaults_to_five_and_stays_in_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RETRIEVAL_TOP_K", raising=False)
    assert Settings(_env_file=None).retrieval_top_k == 5
    monkeypatch.setenv("RETRIEVAL_TOP_K", "8")
    assert Settings(_env_file=None).retrieval_top_k == 8
    for bad in ("0", "21", "-1", "five"):
        monkeypatch.setenv("RETRIEVAL_TOP_K", bad)
        with pytest.raises(ValueError):
            Settings(_env_file=None)

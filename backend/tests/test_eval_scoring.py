"""
Retrieval evaluation, the parts that need no database: reading the question file, the hit rule, the metrics,
and reciprocal rank fusion (docs/EVAL.md, "Method").
"""

import uuid
from pathlib import Path

import pytest

from grounded_answers.evaluation.questions import (
    ExpectedPair,
    QuestionFileError,
    load_questions,
)
from grounded_answers.evaluation.scoring import (
    ScoredChunk,
    first_hit_rank,
    is_hit,
    metrics,
    percentile,
    recall_and_mrr_at_k,
    separation,
)
from grounded_answers.retrieval.rankers import RankedRow, fuse, vector_literal

QUESTIONS = (
    Path(__file__).resolve().parents[2] / "demo-data" / "indus-freight" / "eval-questions.json"
)
CURRENT = {"IFL-HR-001": "3.0", "IFL-FIN-007": "2.0"}


def _chunk(
    document: str, version: str, labels: tuple[str, ...], position: int = 0, score: float = 0.5
) -> ScoredChunk:
    return ScoredChunk(document, version, position, frozenset(labels), score)


def test_the_real_question_file_has_forty_answerable_questions_and_forty_five_pairs() -> None:
    questions = load_questions(QUESTIONS)
    assert len(questions) == 50
    assert sum(q.answerable for q in questions) == 40
    assert sum(len(q.pairs) for q in questions) == 45
    assert all(q.pairs == () for q in questions if not q.answerable)
    assert all(q.pairs for q in questions if q.answerable)


def test_a_question_with_a_version_on_each_clause_gives_two_pairs() -> None:
    by_id = {q.id: q for q in load_questions(QUESTIONS)}
    assert by_id["q045"].pairs == (
        ExpectedPair("IFL-FIN-007", "1.5.1", "2.0"),
        ExpectedPair("IFL-FIN-007", "6.1", "1.0"),
    )
    assert by_id["q047"].pairs == (
        ExpectedPair("IFL-OPS-003", "6.6", None),
        ExpectedPair("IFL-FIN-007", "11.2", None),
    )


def test_a_source_that_cannot_be_read_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    path.write_text(
        '[{"id": "q1", "question": "x", "type": "fact", '
        '"source_document": "IFL-HR-001", "source_section": "see section three"}]',
        encoding="utf-8",
    )
    with pytest.raises(QuestionFileError):
        load_questions(path)


def test_a_clause_matches_only_as_a_whole_label() -> None:
    pair = ExpectedPair("IFL-HR-001", "3.2", None)
    assert not is_hit(pair, _chunk("IFL-HR-001", "3.0", ("3.2.2",)), CURRENT)
    assert is_hit(pair, _chunk("IFL-HR-001", "3.0", ("3.2", "3.2.2")), CURRENT)


def test_a_hit_needs_the_expected_document() -> None:
    pair = ExpectedPair("IFL-HR-001", "3.2", None)
    assert not is_hit(pair, _chunk("IFL-HR-002", "2.0", ("3.2",)), CURRENT)


def test_a_chunk_of_a_retired_version_is_never_a_hit() -> None:
    old = _chunk("IFL-FIN-007", "1.0", ("6.1",))
    assert not is_hit(ExpectedPair("IFL-FIN-007", "6.1", "1.0"), old, CURRENT)
    assert not is_hit(ExpectedPair("IFL-FIN-007", "6.1", None), old, CURRENT)


def test_a_pair_that_names_a_version_needs_that_version() -> None:
    current = _chunk("IFL-FIN-007", "2.0", ("6.1",))
    assert not is_hit(ExpectedPair("IFL-FIN-007", "6.1", "1.0"), current, CURRENT)
    assert is_hit(ExpectedPair("IFL-FIN-007", "6.1", "2.0"), current, CURRENT)


def test_the_first_hit_rank_is_one_based_and_none_without_a_hit() -> None:
    pair = ExpectedPair("IFL-HR-001", "3.2", None)
    ranked = [
        _chunk("IFL-HR-001", "3.0", ("1.1",)),
        _chunk("IFL-HR-001", "3.0", ("3.2",), position=1),
        _chunk("IFL-HR-001", "3.0", ("3.2",), position=2),
    ]
    assert first_hit_rank(pair, ranked, CURRENT) == 2
    assert first_hit_rank(pair, ranked[:1], CURRENT) is None


def test_recall_and_mrr_over_pairs() -> None:
    # Ranks 1, 3, 6 and no hit, with k = 5: two of four pairs are in the top 5.
    result = metrics([1, 3, 6, None], k=5)
    assert result.pairs == 4
    assert result.recall_at_k == pytest.approx(0.5)
    assert result.mrr == pytest.approx((1 + 1 / 3 + 1 / 6) / 4)


def test_metrics_of_nothing_are_zero() -> None:
    assert metrics([], k=5).pairs == 0


def test_nearest_rank_percentile() -> None:
    values = [float(n) for n in range(1, 21)]
    assert percentile(values, 50) == 10.0
    assert percentile(values, 95) == 19.0
    with pytest.raises(ValueError):
        percentile([], 50)


def test_separation_is_one_when_groups_do_not_overlap_and_a_half_for_ties() -> None:
    assert separation([0.9, 0.8], [0.5, 0.4]) == pytest.approx(1.0)
    assert separation([0.5], [0.5]) == pytest.approx(0.5)
    assert separation([0.4], [0.9]) == pytest.approx(0.0)


def test_fusion_adds_reciprocal_ranks_and_breaks_ties_by_version_and_position() -> None:
    version = uuid.UUID(int=1)
    a, b, c = (RankedRow(version, n, (), 0.0) for n in (0, 1, 2))
    fused = fuse([[a, b], [b, c]], depth=10)
    # b is first in one list and second in the other, so it leads; a and c each have one 1/(60+rank) term.
    assert [row.position for row in fused] == [1, 0, 2]
    assert fused[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[1].score == pytest.approx(1 / 61)
    assert fused[2].score == pytest.approx(1 / 62)
    assert [row.position for row in fuse([[a, b], [b, c]], depth=2)] == [1, 0]


def test_fusion_ties_are_broken_by_the_text_key_before_the_random_ids() -> None:
    # Two chunks with equal fused scores. Their version ids sort one way, their text keys the other way.
    low_id, high_id = uuid.UUID(int=1), uuid.UUID(int=2)
    first_by_text = RankedRow(high_id, 0, (), 0.0, tie_key="a")
    second_by_text = RankedRow(low_id, 0, (), 0.0, tie_key="b")
    fused = fuse([[first_by_text], [second_by_text]], depth=10)
    assert [row.tie_key for row in fused] == ["a", "b"]
    assert fused[0].score == fused[1].score


def test_vector_literal_is_pgvector_text() -> None:
    assert vector_literal([0.5, 1.0, -2.0]) == "[0.5,1.0,-2.0]"


def test_mrr_at_k_counts_a_first_hit_below_k_as_zero() -> None:
    # Ranks 1, 3, 6 and no hit. At k = 3 the rank-6 pair and the missing pair are outside.
    recall, mrr = recall_and_mrr_at_k([1, 3, 6, None], k=3)
    assert recall == pytest.approx(0.5)
    assert mrr == pytest.approx((1 + 1 / 3) / 4)
    recall, mrr = recall_and_mrr_at_k([1, 3, 6, None], k=10)
    assert recall == pytest.approx(0.75)
    assert mrr == pytest.approx((1 + 1 / 3 + 1 / 6) / 4)
    assert recall_and_mrr_at_k([], k=5) == (0.0, 0.0)


def test_a_larger_fusion_constant_flattens_the_difference_between_ranks() -> None:
    version = uuid.UUID(int=1)
    a, b = (RankedRow(version, n, (), 0.0) for n in (0, 1))
    small = fuse([[a, b]], depth=10, constant=1)
    large = fuse([[a, b]], depth=10, constant=100)
    assert small[0].score == pytest.approx(1 / 2) and small[1].score == pytest.approx(1 / 3)
    assert large[0].score == pytest.approx(1 / 101) and large[1].score == pytest.approx(1 / 102)
    # The default is the standard 60.
    assert fuse([[a]], depth=10)[0].score == pytest.approx(1 / 61)

"""The pure parts of the embedding measurement (the model run itself is a manual measurement, docs/EVAL.md)."""

from grounded_answers.evaluation.embed_concurrency import MATRIX, memory_mib, percentile


def test_percentile_picks_a_value_from_the_list() -> None:
    values = [float(n) for n in range(1, 101)]
    assert percentile(values, 50) in (50.0, 51.0)
    assert percentile(values, 95) == 95.0 or percentile(values, 95) == 96.0
    assert percentile([7.0], 95) == 7.0


def test_memory_counters_are_positive_and_peak_is_not_below_current() -> None:
    reading = memory_mib()
    assert reading["working_set"] > 0
    assert reading["peak_working_set"] >= reading["working_set"] - 1


def test_the_matrix_covers_one_two_and_four_at_a_time() -> None:
    assert {clients for limit, clients in MATRIX if limit == clients} == {1, 2, 4}

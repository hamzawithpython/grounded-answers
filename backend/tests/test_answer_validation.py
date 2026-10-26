"""
Quote normalisation and citation validation (ADR 0032, decision 2), numeric grounding, and the clause detector
(decision 3). Pure functions: no network, no database.
"""

from typing import Any

import pytest

from grounded_answers.answering.clauses import clause_at, clause_candidates
from grounded_answers.answering.normalise import normalise
from grounded_answers.answering.schema import parse_answer
from grounded_answers.answering.validate import (
    Invalid,
    Valid,
    numbers_in,
    validate_answer,
)
from grounded_answers.retrieval import RetrievedChunk
from tests.answering_support import answer_json, make_chunk

LEAVE = (
    "3.1 Annual leave. Employees receive 20 days of paid annual leave.\n"
    "3.2 Probation - 3.2.1 Probation is 3 months for all employees except drivers. "
    "- 3.2.2 Probation for HTV and LTV drivers is 6 months. See 4.1 and Table 3.1 for details."
)
TABLE = "[table]\nGrade | Daily allowance (PKR)\ng3 | 2,500\ng4 | 3,000\nTrip type | Approver"


def check(chunks: list[RetrievedChunk], **kwargs: Any) -> Valid | Invalid:
    return validate_answer(parse_answer(answer_json(**kwargs)), chunks)


def cite(ref: int, quote: str) -> list[dict[str, object]]:
    return [{"ref": ref, "quote": quote}]


# --- normalisation ------------------------------------------------------------------------------------------


def test_normalisation_folds_case_whitespace_quotes_dashes_and_table_pipes() -> None:
    nbsp, en_dash, rsq, ldq, rdq = (chr(c) for c in (0xA0, 0x2013, 0x2019, 0x201C, 0x201D))
    text, offsets = normalise(
        f"  Drivers{nbsp}MUST\n\n rest {en_dash} it{rsq}s {ldq}ten{rdq} | hours  "
    )
    # The spaced dash is a lone list-marker token and is dropped; a hyphen inside a word is kept (next test).
    assert text == 'drivers must rest it\'s "ten" hours'
    assert len(offsets) == len(text)


def test_a_lone_list_marker_is_dropped_but_a_hyphen_inside_a_word_is_not() -> None:
    assert normalise("drivers. - 3.2.2 Probation")[0] == "drivers. 3.2.2 probation"
    assert normalise("rules-3.4.1 Speed and 24-hour")[0] == "rules-3.4.1 speed and 24-hour"


def test_offsets_point_back_into_the_original_text() -> None:
    original = "AB\n\n  cd EF"
    text, offsets = normalise(original)
    at = text.find("cd")
    assert original[offsets[at] : offsets[at] + 2] == "cd"


# --- citations ----------------------------------------------------------------------------------------------


def test_a_good_answer_with_an_exact_quote_is_valid_and_gets_its_clause() -> None:
    result = check(
        [make_chunk(LEAVE, section_number="3")],
        answer="Probation for drivers is 6 months.",
        citations=cite(1, "Probation for HTV and LTV drivers is 6 months."),
    )
    assert isinstance(result, Valid)
    assert result.citations[0].location.label == "3.2.2"
    assert result.citations[0].location.level == "clause"


def test_the_quote_may_differ_in_case_whitespace_and_may_cross_a_joined_list_marker() -> None:
    result = check(
        [make_chunk(LEAVE, section_number="3")],
        answer="3 months, or 6 months for drivers.",
        citations=cite(
            1,
            "PROBATION IS 3 months for all employees\nexcept drivers. 3.2.2 Probation for HTV and LTV drivers is 6 months.",
        ),
    )
    assert isinstance(result, Valid)


@pytest.mark.parametrize("ref", [0, 3, -1, 99])
def test_a_forged_ref_invalidates_the_answer(ref: int) -> None:
    chunks = [
        make_chunk(LEAVE, section_number="3"),
        make_chunk("Other chunk text that is long enough."),
    ]
    result = check(chunks, citations=cite(ref, "Probation for HTV and LTV drivers is 6 months."))
    assert result == Invalid("bad_ref")


def test_a_quote_that_is_not_in_the_chunk_invalidates_the_answer() -> None:
    result = check(
        [make_chunk(LEAVE, section_number="3")],
        citations=cite(1, "Probation is 12 months for everyone."),
    )
    assert result == Invalid("quote_not_found")


def test_a_quote_that_exists_only_in_another_chunk_is_not_found_for_this_ref() -> None:
    chunks = [
        make_chunk("Overtime is paid at one and a half times."),
        make_chunk(LEAVE, section_number="3"),
    ]
    result = check(chunks, citations=cite(1, "Probation for HTV and LTV drivers is 6 months."))
    assert result == Invalid("quote_not_found")


def test_one_bad_citation_among_good_ones_invalidates_the_whole_answer() -> None:
    chunk = make_chunk(LEAVE, section_number="3")
    citations = [
        {"ref": 1, "quote": "Probation for HTV and LTV drivers is 6 months."},
        {"ref": 1, "quote": "this quote was never in the document at all"},
    ]
    assert check([chunk], answer="Drivers: 6 months.", citations=citations) == Invalid(
        "quote_not_found"
    )


@pytest.mark.parametrize(
    "quote",
    ["Probation for HTV ... 6 months", "Probation for HTV " + chr(0x2026) + " 6 months drivers"],
)
def test_an_ellipsis_quote_is_rejected(quote: str) -> None:
    assert check([make_chunk(LEAVE, section_number="3")], citations=cite(1, quote)) == Invalid(
        "quote_ellipsis"
    )


def test_text_quotes_need_twenty_characters_and_at_most_five_hundred() -> None:
    chunk = make_chunk(LEAVE, section_number="3")
    assert check([chunk], citations=cite(1, "paid annual leave")) == Invalid("quote_too_short")
    assert isinstance(check([chunk], citations=cite(1, "20 days of paid annual leave")), Valid)
    long_chunk = make_chunk("word " * 200)
    assert check([long_chunk], citations=cite(1, "word " * 120)) == Invalid("quote_too_long")


def test_empty_answers_and_missing_citations_are_invalid() -> None:
    chunk = make_chunk(LEAVE, section_number="3")
    assert check(
        [chunk], answer="  ", citations=cite(1, "20 days of paid annual leave")
    ) == Invalid("empty_answer")
    assert check([chunk], answer="Twenty days.", citations=[]) == Invalid("no_citations")


def test_duplicate_citations_are_merged() -> None:
    chunk = make_chunk(LEAVE, section_number="3")
    quote = "20 days of paid annual leave"
    result = check([chunk], answer="20 days.", citations=cite(1, quote) + cite(1, quote.upper()))
    assert isinstance(result, Valid) and len(result.citations) == 1


# --- table quotes -------------------------------------------------------------------------------------------


def test_a_short_table_quote_with_a_row_label_and_a_value_is_valid() -> None:
    result = check(
        [make_chunk(TABLE, section_number="10")],
        answer="Grade g3 gets 2,500.",
        citations=cite(1, "g3 2,500"),
    )
    assert isinstance(result, Valid)
    assert result.citations[0].location.level == "table"
    assert result.citations[0].location.label == "10"


def test_a_table_quote_of_only_numbers_is_not_informative() -> None:
    chunk = make_chunk(TABLE, section_number="10")
    assert check([chunk], answer="It is 2,500.", citations=cite(1, "2,500 3,000")) == Invalid(
        "quote_not_informative"
    )


def test_a_table_quote_under_eight_characters_is_too_short() -> None:
    assert check([make_chunk(TABLE, section_number="10")], citations=cite(1, "g3 2,5")) == Invalid(
        "quote_too_short"
    )


def test_the_same_short_quote_is_too_short_in_a_text_chunk() -> None:
    chunk = make_chunk("Grade g3 gets 2,500 per day for meals and lodging.", section_number="10")
    assert check([chunk], answer="2,500.", citations=cite(1, "g3 gets 2,500")) == Invalid(
        "quote_too_short"
    )


# --- numeric grounding --------------------------------------------------------------------------------------


def test_a_number_in_the_answer_that_the_quote_lacks_is_invalid() -> None:
    chunk = make_chunk(LEAVE, section_number="3")
    result = check(
        [chunk],
        answer="Drivers have 9 months of probation.",
        citations=cite(1, "Probation for HTV and LTV drivers is 6 months."),
    )
    assert result == Invalid("number_not_grounded")


def test_a_number_that_is_in_the_quote_passes_including_thousands_separators() -> None:
    chunk = make_chunk(TABLE, section_number="10")
    assert isinstance(
        check([chunk], answer="Grade g3 receives PKR 2500 a day.", citations=cite(1, "g3 2,500")),
        Valid,
    )
    assert isinstance(
        check([chunk], answer="Grade g3 receives PKR 2,500 a day.", citations=cite(1, "g3 2,500")),
        Valid,
    )


def test_numbers_may_come_from_any_cited_quote() -> None:
    chunk = make_chunk(LEAVE, section_number="3")
    citations = cite(1, "20 days of paid annual leave") + cite(
        1, "Probation for HTV and LTV drivers is 6 months."
    )
    assert isinstance(
        check([chunk], answer="20 days leave; probation 6 months.", citations=citations), Valid
    )


def test_clause_labels_and_refs_in_the_answer_text_are_not_numbers_to_ground() -> None:
    chunk = make_chunk(LEAVE, section_number="3")
    answer = "Per clause 3.2.2 and section 3.2 [1], drivers serve 6 months (see Table 3.1)."
    result = check(
        [chunk], answer=answer, citations=cite(1, "Probation for HTV and LTV drivers is 6 months.")
    )
    assert isinstance(result, Valid)


def test_numbers_in_extracts_digits_only_and_ignores_labels_when_asked() -> None:
    assert numbers_in(
        "Pay 2,500 or 1.5 times in 3.2.2 [4], section 7.1, Table 9", drop_labels=True
    ) == {"2500", "1.5"}
    assert numbers_in("12% and 2,500", drop_labels=False) == {"12", "2500"}


# --- clauses ------------------------------------------------------------------------------------------------


def test_candidates_keep_own_section_labels_and_drop_cross_references() -> None:
    labels = [label for _, label in clause_candidates(LEAVE, "3")]
    assert labels == ["3.1", "3.2", "3.2.1", "3.2.2"]  # not 4.1 (other section) and not "Table 3.1"


def test_a_label_of_another_section_is_dropped_even_without_a_cross_reference_word() -> None:
    body = "3.1 Intro text. The budget (4.7.2) is separate. 3.2 More rules."
    assert [label for _, label in clause_candidates(body, "3")] == ["3.1", "3.2"]


def test_a_quote_takes_the_nearest_preceding_label() -> None:
    at = LEAVE.index("Probation is 3 months")
    assert clause_at(LEAVE, "3", at).label == "3.2.1"
    assert clause_at(LEAVE, "3", LEAVE.index("Employees receive")).label == "3.1"


def test_a_quote_before_the_first_label_falls_back_to_the_section() -> None:
    body = "continued from the previous chunk. 4.2.3 Night trips need two drivers."
    assert (clause_at(body, "4", 5).level, clause_at(body, "4", 5).label) == ("section", "4")
    assert clause_at(body, "4", body.index("Night")).label == "4.2.3"


def test_a_chunk_without_a_section_number_or_a_table_is_never_clause_level() -> None:
    assert clause_at("1.1 text", None, 3).level == "none"
    assert clause_at(TABLE, "10", 3).level == "table"
    assert clause_candidates(TABLE, "10") == []


def test_a_label_after_a_pdf_joined_hyphen_counts() -> None:
    body = "3.4 Operating rules-3.4.1 Speed limits: 8 km/h in the yard."
    assert [label for _, label in clause_candidates(body, "3")] == ["3.4", "3.4.1"]

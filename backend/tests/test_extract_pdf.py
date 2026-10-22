"""PDF extraction into the common structure: heading detection, levels, pages, no-text (ADR 0020)."""

from grounded_answers.extract.pdf import extract_pdf
from tests.extract_fixtures import blank_pdf, pdf_pages

BODY = 12
HEADING = 16
TITLE = 20


def _sections(result: object) -> list[tuple[str | None, str, int, int | None]]:
    return [(s.number, s.heading, s.level, s.page) for s in result.sections]  # type: ignore[attr-defined]


def test_numbered_headings_take_their_level_from_the_number_and_the_title_from_its_size() -> None:
    pdf = pdf_pages(
        [
            [
                (TITLE, "Driver Handbook"),
                (HEADING, "4 Hours of Driving"),
                (BODY, "Drivers keep a log of every shift."),
                (HEADING, "4.2 Rest Breaks"),
                (BODY, "A break is due after five hours."),
            ]
        ]
    )
    result = extract_pdf(pdf)
    assert _sections(result) == [
        (None, "Driver Handbook", 1, 1),
        ("4", "Hours of Driving", 1, 1),
        ("4.2", "Rest Breaks", 2, 1),
    ]
    rest = result.sections[2]
    assert rest.text == "A break is due after five hours."


def test_a_heading_on_a_later_page_carries_that_page_number() -> None:
    pdf = pdf_pages(
        [
            [(TITLE, "Policy"), (BODY, "Intro.")],
            [(HEADING, "5 Vehicle Checks"), (BODY, "Check tyres daily.")],
        ]
    )
    result = extract_pdf(pdf)
    assert result.page_count == 2
    checks = next(section for section in result.sections if section.number == "5")
    assert checks.page == 2


def test_body_text_at_the_heading_size_is_not_a_heading_when_it_is_long() -> None:
    long_line = "This sentence is set in a larger font but it is far too long to be a heading " * 2
    pdf = pdf_pages([[(HEADING, long_line), (BODY, "Short body.")]])
    result = extract_pdf(pdf)
    assert all(section.heading == "" for section in result.sections)


def test_tables_are_kept_as_text_lines_in_v1() -> None:
    """D-A: no table structure for PDF in v1. The cell text is present, in order."""
    pdf = pdf_pages([[(TITLE, "Breaks"), (BODY, "Shift Break"), (BODY, "Day 30 minutes")]])
    result = extract_pdf(pdf)
    assert "Shift Break" in result.sections[0].text
    assert all(section.tables == () for section in result.sections)


def test_a_pdf_with_no_text_has_no_sections_and_no_text() -> None:
    """D-C: a scan (no text layer) is refused by the caller as no_text. OCR is out of scope."""
    result = extract_pdf(blank_pdf())
    assert result.sections == ()
    assert not result.has_text()
    assert result.page_count == 1

"""
The extraction sandbox: a child process with a timeout, an output cap and (on Linux) a memory limit
(ADR 0020, decisions D-D and the stderr rule). Real child processes; no database.
"""

import logging
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from grounded_answers.extract import sandbox
from grounded_answers.extract.sandbox import ExtractionFailed, extract
from tests.extract_fixtures import (
    assemble,
    blank_pdf,
    docx_with_headings,
    pdf_pages,
    stream_bomb,
)

LINUX_ONLY = pytest.mark.skipif(
    sys.platform != "linux",
    reason="RLIMIT_AS is Linux-only; this test runs in the CI Linux job (ADR 0020, D-D)",
)
MARKER = "MARKER-7f3a9-must-not-appear"


def _python_child(code: str) -> Any:
    """A replacement child command: run the given code under -I, with the file on stdin."""

    def command(kind: str) -> list[str]:
        return [sys.executable, "-I", "-c", code]

    return command


def _deep_nesting_pdf() -> bytes:
    """A valid PDF whose content stream is 100,000 nested arrays: pypdf takes over a minute on it."""
    content = b"[" * 100_000 + b"]" * 100_000
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    return assemble(objects)


def test_a_pdf_is_extracted_through_the_child_process() -> None:
    result = extract("pdf", pdf_pages([[(20, "Driver Handbook"), (12, "Body text here.")]]))
    assert result.format == "pdf"
    assert result.sections[0].heading == "Driver Handbook"


def test_a_docx_is_extracted_through_the_child_process() -> None:
    result = extract("docx", docx_with_headings())
    assert any(section.number == "4.2" for section in result.sections)


def test_a_markdown_file_is_extracted_through_the_child_process() -> None:
    result = extract("md", b"# Title\n\n## 4.2 Rest\n\nText.")
    assert [s.number for s in result.sections if s.number] == ["4.2"]


def test_a_pdf_with_no_text_is_refused_as_no_text() -> None:
    with pytest.raises(ExtractionFailed) as refused:
        extract("pdf", blank_pdf())
    assert refused.value.reason == "no_text"


def test_bytes_that_are_not_a_pdf_are_refused_as_unreadable() -> None:
    with pytest.raises(ExtractionFailed) as refused:
        extract("pdf", b"%PDF-1.4 but then nothing that parses")
    assert refused.value.reason == "unreadable"


def test_an_unknown_kind_is_refused_before_any_child_starts() -> None:
    with pytest.raises(ValueError):
        extract("exe", b"MZ")


def test_a_child_that_runs_too_long_is_killed_at_its_timeout(monkeypatch: MonkeyPatch) -> None:
    """The mechanism: a child that sleeps for a minute is stopped at 2 s and reported as timeout."""
    monkeypatch.setitem(sandbox.TIMEOUT_SECONDS, "md", 2.0)
    monkeypatch.setattr(sandbox, "_child_command", _python_child("import time; time.sleep(60)"))
    started = time.monotonic()
    with pytest.raises(ExtractionFailed) as refused:
        extract("md", b"x")
    assert refused.value.reason == "timeout"
    assert time.monotonic() - started < 20, "the child must be stopped, not waited for"


def test_a_deeply_nested_pdf_is_stopped_or_refused_within_the_limit() -> None:
    """
    The real file: pypdf raises RecursionError on it at once, so the child reports unreadable.
    Some other nesting can make a parser run for much longer: the timeout above covers that case.
    """
    started = time.monotonic()
    with pytest.raises(ExtractionFailed) as refused:
        extract("pdf", _deep_nesting_pdf())
    assert refused.value.reason in {"timeout", "unreadable"}
    assert time.monotonic() - started < 60


def test_a_child_that_writes_too_much_output_is_stopped_and_refused(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "MAX_OUTPUT_BYTES", 1000)
    monkeypatch.setattr(
        sandbox, "_child_command", _python_child("import sys; sys.stdout.write('x' * 100000)")
    )
    with pytest.raises(ExtractionFailed) as refused:
        extract("md", b"x")
    assert refused.value.reason == "unreadable"


def test_stderr_text_never_reaches_the_logs_or_the_reason(
    monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A child that writes the marker to stderr and fails. The marker must appear in neither place."""
    code = (
        "import sys; sys.stderr.write('Traceback: " + MARKER + "'); "
        'sys.stdout.write(\'{"ok": false, "reason": "unreadable", "exception": "ValueError"}\')'
    )
    monkeypatch.setattr(sandbox, "_child_command", _python_child(code))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ExtractionFailed) as refused:
        extract("md", b"x")
    assert refused.value.reason == "unreadable"
    assert MARKER not in str(refused.value)
    assert MARKER not in caplog.text


def test_a_document_that_makes_the_child_fail_leaks_nothing_into_logs_or_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real path: a document carrying the marker is not a valid PDF, so the child fails on it."""
    caplog.set_level(logging.DEBUG)
    document = b"%PDF-1.4\n" + MARKER.encode() + b"\n" + bytes(range(256)) * 40
    with pytest.raises(ExtractionFailed) as refused:
        extract("pdf", document)
    assert refused.value.reason in {"unreadable", "no_text"}
    assert MARKER not in str(refused.value)
    assert MARKER not in caplog.text


def test_the_failure_log_line_names_the_reason_and_the_exception_class_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with pytest.raises(ExtractionFailed):
        extract("pdf", b"%PDF-1.4 unreadable")
    lines = [record.getMessage() for record in caplog.records if record.name == sandbox.logger.name]
    assert any(line.startswith("extract.failed reason=unreadable exception=") for line in lines)


def test_a_real_pdf_bomb_is_refused_without_its_inflated_content(tmp_path: Path) -> None:
    """
    The production path: pypdf refuses the bomb at its own decompression limit (a LimitReached error),
    so the child returns unreadable and no structure. This does not depend on the platform.
    """
    inflated = b"BT /F1 9 Tf 72 720 Td " + b"(A) Tj " * 14_000_000 + b" ET\n"
    import zlib

    body = zlib.compress(inflated, 9)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(body)).encode()
        + b" /Filter /FlateDecode >>\nstream\n"
        + body
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    from tests.extract_fixtures import assemble

    with pytest.raises(ExtractionFailed) as refused:
        extract("pdf", assemble(objects))
    assert refused.value.reason == "unreadable"


@LINUX_ONLY
def test_the_linux_memory_limit_turns_an_inflated_bomb_into_a_memory_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    """
    The stdlib memory limit (RLIMIT_AS, 256 MiB) stops a decompression that would need 512 MiB.
    The child does the decompression itself, with no parser in between, so the limit is what stops it.
    """
    limit = 256 * 1024 * 1024
    code = (
        "import json, resource, sys, zlib;"
        f"resource.setrlimit(resource.RLIMIT_AS, ({limit}, {limit}));"
        "data = sys.stdin.buffer.read();"
        "\ntry:\n    zlib.decompress(data)\n"
        "except MemoryError:\n"
        "    sys.stdout.write(json.dumps({'ok': False, 'reason': 'memory', 'exception': 'MemoryError'}))\n"
        "else:\n"
        "    sys.stdout.write(json.dumps({'ok': False, 'reason': 'unreadable', 'exception': 'NotBlocked'}))\n"
    )
    monkeypatch.setattr(sandbox, "_child_command", _python_child(code))
    with pytest.raises(ExtractionFailed) as refused:
        extract("md", stream_bomb(512 * 1024 * 1024))
    assert refused.value.reason == "memory"


@LINUX_ONLY
def test_the_child_uses_the_memory_limit_argument_on_linux() -> None:
    command = sandbox._child_command("pdf")
    assert command[-1] == str(sandbox.MEMORY_LIMIT_BYTES)


@pytest.mark.skipif(
    sys.platform == "linux", reason="this check is for the Windows development path"
)
def test_on_windows_the_child_runs_with_a_timeout_only() -> None:
    command = sandbox._child_command("pdf")
    assert str(sandbox.MEMORY_LIMIT_BYTES) not in command

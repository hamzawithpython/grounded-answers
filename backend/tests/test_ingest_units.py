"""
Ingestion units that need no database (ADR 0022): the S1 chunker, the pinned model files, the fetch step, the
retry classification, and the lease setting.
"""

import hashlib
import io
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _pytest.monkeypatch import MonkeyPatch
from pydantic import ValidationError

import grounded_answers.embedding.fetch as fetch
from grounded_answers.embedding import model as model_module
from grounded_answers.embedding.hashing import sha256_file
from grounded_answers.embedding.model import ModelIntegrityError, verify_model_dir
from grounded_answers.embedding.pinned import (
    MODEL_NAME,
    MODEL_REVISION,
    PASSAGE_PREFIX,
    PINNED_FILES,
)
from grounded_answers.extract.model import Extracted, Section, Table
from grounded_answers.ingest.chunking import (
    MAX_BODY_CHARS,
    MAX_CHARS,
    ChunkTooLong,
    embedding_text,
    section_chunks,
)
from grounded_answers.ingest.failures import LeaseHeld, VersionNotFound
from grounded_answers.jobs.ingest_task import RETRYABLE, attempt_token
from grounded_answers.worker_config import WorkerSettings


def _section(
    number: str | None, heading: str, text: str, tables: tuple[Table, ...] = ()
) -> Section:
    return Section(number=number, heading=heading, level=1, page=None, text=text, tables=tables)


def test_section_text_is_split_into_pieces_of_at_most_max_chars() -> None:
    lines = "\n".join(f"3.{i} clause text number {i} " + "x" * 80 for i in range(1, 60))
    chunks = section_chunks(
        Extracted(format="md", page_count=None, sections=(_section("3", "Rules", lines),))
    )
    assert len(chunks) > 1
    assert all(len(chunk.body) <= MAX_CHARS for chunk in chunks)


def test_a_single_line_longer_than_the_limit_is_cut_so_no_chunk_is_too_long() -> None:
    long_line = "word " * 1000
    chunks = section_chunks(
        Extracted(format="md", page_count=None, sections=(_section("1", "Long", long_line),))
    )
    assert chunks and all(len(chunk.body) <= MAX_CHARS for chunk in chunks)


def test_a_table_is_one_chunk_even_when_it_is_long() -> None:
    rows = [["Grade", "Level", "Notes"]] + [[f"G{i}", str(i), "x" * 60] for i in range(40)]
    section = _section(
        "6", "Gifts", "6.2 Limits.", tables=(Table(rows=tuple(tuple(row) for row in rows)),)
    )
    chunks = section_chunks(Extracted(format="md", page_count=None, sections=(section,)))
    table_chunks = [chunk for chunk in chunks if chunk.body.startswith("[table]")]
    assert len(table_chunks) == 1
    assert "G39" in table_chunks[0].body


def test_clause_labels_list_the_dotted_clauses_in_the_body_and_the_section_number() -> None:
    body = "3.2 Probation is six months.\n3.2.2 A road assessment is due. Version 2.0 applies."
    chunks = section_chunks(
        Extracted(format="md", page_count=None, sections=(_section("3", "Probation", body),))
    )
    labels = set(chunks[0].clause_labels)
    assert {"3", "3.2", "3.2.2"} <= labels
    assert "2.0" in labels, (
        "any dotted number is recorded; a citation looks up one label, so extras are harmless"
    )


def test_a_chunk_body_over_the_stored_limit_is_refused_not_truncated() -> None:
    huge = "\n".join("y" * 100 for _ in range(MAX_BODY_CHARS // 50))
    table = Table(rows=(("a",), (huge,)))
    with pytest.raises(ChunkTooLong):
        section_chunks(
            Extracted(
                format="md", page_count=None, sections=(_section("1", "Big", "", tables=(table,)),)
            )
        )


def test_empty_sections_produce_no_chunks_and_positions_are_sequential() -> None:
    sections = (
        _section("1", "Empty", "   \n  "),
        _section("2", "Two", "2.1 first\n2.2 second"),
        _section("3", "Three", "3.1 only"),
    )
    chunks = section_chunks(Extracted(format="md", page_count=None, sections=sections))
    assert [chunk.position for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.section_number != "1" for chunk in chunks)


def test_the_embedding_text_names_the_document_version_section_and_heading() -> None:
    chunk = section_chunks(
        Extracted(
            format="md",
            page_count=None,
            sections=(_section("3.2", "Probation", "3.2.2 six months"),),
        )
    )[0]
    text = embedding_text(chunk, "Expense Policy", 2)
    assert text.startswith("Expense Policy v2 | 3.2 Probation\n")
    assert "six months" in text
    assert not text.startswith(PASSAGE_PREFIX), (
        "the embedder adds the passage prefix, not the chunker"
    )


def test_the_pinned_files_are_the_ones_the_loader_needs() -> None:
    assert "onnx/model.onnx" in PINNED_FILES
    assert "tokenizer.json" in PINNED_FILES
    assert len(MODEL_REVISION) == 40 and all(c in "0123456789abcdef" for c in MODEL_REVISION)
    assert MODEL_NAME == "intfloat/multilingual-e5-small"


# A two-file pin table for these tests, so they never write the real 470 MB model. The real table is checked
# against the real files by the DB job (test_embedding_model.py).
SMALL_PINS: dict[str, tuple[str, int]] = {
    "a.bin": (hashlib.sha256(b"abc").hexdigest(), 3),
    "dir/b.json": (hashlib.sha256(b"{}").hexdigest(), 2),
}


@pytest.fixture
def small_pins(monkeypatch: MonkeyPatch) -> dict[str, tuple[str, int]]:
    monkeypatch.setattr(model_module, "PINNED_FILES", SMALL_PINS)
    monkeypatch.setattr(fetch, "PINNED_FILES", SMALL_PINS)
    return SMALL_PINS


def test_verification_refuses_a_missing_model_file(
    tmp_path: Path, small_pins: dict[str, tuple[str, int]]
) -> None:
    (tmp_path / "a.bin").write_bytes(b"abc")
    with pytest.raises(ModelIntegrityError, match=r"missing: dir/b\.json"):
        verify_model_dir(tmp_path)


def test_verification_refuses_a_file_that_differs_from_its_pin(
    tmp_path: Path, small_pins: dict[str, tuple[str, int]]
) -> None:
    (tmp_path / "a.bin").write_bytes(b"xyz")  # the same size as the pin, other bytes
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "b.json").write_bytes(b"{}")
    with pytest.raises(ModelIntegrityError, match=r"does not match its pin: a\.bin"):
        verify_model_dir(tmp_path)


def test_fetch_refuses_a_download_that_does_not_match_its_pin_and_keeps_nothing(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    class FakeResponse(io.BytesIO):
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: Any) -> None:
            self.close()

    def tampered(url: str, timeout: float) -> FakeResponse:
        return FakeResponse(b"not the pinned file")

    monkeypatch.setattr(urllib.request, "urlopen", tampered)
    with pytest.raises(ModelIntegrityError, match="does not match its pin"):
        fetch.fetch_pinned_model(tmp_path)
    assert not any(path.suffix == ".onnx" for path in tmp_path.rglob("*") if path.is_file())
    assert not any(path.name.endswith(".part") for path in tmp_path.rglob("*") if path.is_file())


def test_a_file_already_in_the_cache_is_checked_not_replaced(
    tmp_path: Path, small_pins: dict[str, tuple[str, int]]
) -> None:
    directory = fetch.model_dir(tmp_path)
    (directory / "dir").mkdir(parents=True)
    (directory / "a.bin").write_bytes(
        b"xyz"
    )  # cached, and wrong: it must be refused, never replaced
    (directory / "dir" / "b.json").write_bytes(b"{}")
    with pytest.raises(ModelIntegrityError, match=r"does not match its pin: a\.bin"):
        fetch.fetch_pinned_model(tmp_path)
    assert (directory / "a.bin").read_bytes() == b"xyz"


def test_sha256_helper_matches_the_standard_library(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "f.bin"
    path.write_bytes(b"abc")
    assert sha256_file(path) == hashlib.sha256(b"abc").hexdigest()


def test_only_transient_errors_are_retried() -> None:
    assert issubclass(LeaseHeld, RETRYABLE)
    assert not issubclass(VersionNotFound, RETRYABLE)
    assert not issubclass(ValueError, RETRYABLE)


def test_a_job_keeps_its_token_across_retries_and_another_job_gets_a_different_one() -> None:
    assert attempt_token(7) == attempt_token(7)
    assert attempt_token(7) != attempt_token(8)
    assert isinstance(attempt_token(7), uuid.UUID)


def test_the_processing_lease_is_bounded(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_DATABASE_URL", "postgresql://worker_user:x@127.0.0.1:5432/db")
    assert WorkerSettings().worker_processing_lease_seconds == 1200
    monkeypatch.setenv("WORKER_PROCESSING_LEASE_SECONDS", "10")
    with pytest.raises(ValidationError):
        WorkerSettings()


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: MonkeyPatch) -> Iterator[None]:
    """Unit tests never open a socket. Anything that tries fails loudly."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("unit tests must not use the network")

    monkeypatch.setattr("socket.socket.connect", refuse)
    yield

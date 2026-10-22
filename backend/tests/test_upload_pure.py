"""Filename, type detection and the streaming body cap for uploads (ADR 0019). No database."""

import io
import unicodedata
import zipfile
from collections.abc import AsyncIterator, Iterable, MutableMapping
from typing import Any

import pytest

from grounded_answers.upload import detect
from grounded_answers.upload.body_limit import UploadBodyLimit
from grounded_answers.upload.detect import Kind, UnsupportedFileType, detect_kind, inspect_docx
from grounded_answers.upload.limits import MAX_ZIP_ENTRIES
from grounded_answers.upload.sanitize import FALLBACK_NAME, MAX_FILENAME_CHARS, sanitize_filename

# ---- sanitize_filename --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Windows\\win.ini", "win.ini"),
        ("folder/sub\\report.pdf", "report.pdf"),
        ("..", FALLBACK_NAME),
        (".", FALLBACK_NAME),
        ("", FALLBACK_NAME),
        ("\x00\x01\x02", FALLBACK_NAME),
        ("a\x00b.pdf", "ab.pdf"),
        ("a\nb\tc.txt", "a b c.txt"),
        ("  spaced   name  .pdf  ", "spaced name .pdf"),
    ],
    ids=[
        "unix-traversal",
        "windows-traversal",
        "mixed-separators",
        "dotdot",
        "dot",
        "empty",
        "only-controls",
        "nul",
        "line-breaks",
        "spaces",
    ],
)
def test_path_and_control_characters_are_removed(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_right_to_left_override_is_removed() -> None:
    """U+202E reverses how the rest of a name is shown ('invoice.exe' can look like 'invoice.pdf')."""
    name = sanitize_filename("invoice\u202egpj.exe")
    assert "\u202e" not in name
    assert name == "invoicegpj.exe"


def test_all_bidirectional_controls_are_removed() -> None:
    controls = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f"
    assert sanitize_filename(f"a{controls}b.pdf") == "ab.pdf"


@pytest.mark.parametrize(
    "raw",
    ["fi\u200ble.pdf", "file\ufeff.pdf", "fi\u200dle.pdf", "fi\u2060le.pdf"],
    ids=["zero-width-space", "byte-order-mark", "zero-width-joiner", "word-joiner"],
)
def test_zero_width_characters_are_removed(raw: str) -> None:
    assert sanitize_filename(raw) == "file.pdf"


def test_unicode_letters_are_kept_and_normalised_to_nfc() -> None:
    decomposed = unicodedata.normalize("NFD", "cafÃ©.pdf")
    assert sanitize_filename(decomposed) == "cafÃ©.pdf"
    assert sanitize_filename("à¤¨à¥€à¤¤à¤¿.pdf") == "à¤¨à¥€à¤¤à¤¿.pdf"
    assert sanitize_filename("Politique_sÃ©curitÃ©.pdf") == "Politique_sÃ©curitÃ©.pdf"


def test_a_long_name_is_cut_to_the_database_limit() -> None:
    assert len(sanitize_filename("a" * 400 + ".pdf")) == MAX_FILENAME_CHARS


# ---- detect_kind ----------------------------------------------------------------------------------


def _docx(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


GOOD_DOCX = _docx({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"})


def test_a_pdf_is_recognised_by_its_magic() -> None:
    assert detect_kind(b"%PDF-1.7\n...", "policy.pdf") is Kind.PDF


def test_a_pdf_without_the_magic_is_refused() -> None:
    with pytest.raises(UnsupportedFileType):
        detect_kind(b"just some text", "policy.pdf")


def test_a_pdf_renamed_to_txt_is_refused() -> None:
    """A PDF is mostly ASCII and may even decode as UTF-8, so the magic check must still refuse it."""
    with pytest.raises(UnsupportedFileType):
        detect_kind(b"%PDF-1.4 plain looking text", "notes.txt")


def test_a_docx_is_recognised_by_its_parts() -> None:
    assert detect_kind(GOOD_DOCX, "policy.docx") is Kind.DOCX


def test_a_zip_without_the_docx_parts_is_refused() -> None:
    not_a_docx = _docx({"readme.txt": b"hello"})
    with pytest.raises(UnsupportedFileType):
        detect_kind(not_a_docx, "policy.docx")


def test_bytes_that_are_not_a_zip_are_refused_as_a_docx() -> None:
    with pytest.raises(UnsupportedFileType):
        detect_kind(b"PK\x03\x04 broken zip header", "policy.docx")


def test_a_pdf_renamed_to_docx_is_refused() -> None:
    with pytest.raises(UnsupportedFileType):
        detect_kind(b"%PDF-1.7", "policy.docx")


def test_a_zip_with_too_many_entries_is_refused() -> None:
    entries = {f"f{i}.txt": b"x" for i in range(MAX_ZIP_ENTRIES + 1)}
    entries.update({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"})
    with pytest.raises(UnsupportedFileType):
        inspect_docx(_docx(entries))


def test_a_zip_whose_declared_total_is_too_large_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detect, "MAX_ZIP_TOTAL_BYTES", 100)
    with pytest.raises(UnsupportedFileType):
        inspect_docx(_docx({"[Content_Types].xml": b"x" * 80, "word/document.xml": b"y" * 80}))


def test_a_zip_entry_that_is_too_large_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect, "MAX_ZIP_ENTRY_BYTES", 100)
    with pytest.raises(UnsupportedFileType):
        inspect_docx(_docx({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"y" * 200}))


def test_an_encrypted_entry_is_refused() -> None:
    """zipfile clears the encryption bit when it writes, so set it in the central directory after."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", b"<w:document/>")
    data = bytearray(buffer.getvalue())
    central = data.index(b"PK\x01\x02")
    data[central + 8] |= 0x01  # general purpose bit flag, low byte: bit 0 is "encrypted"
    with pytest.raises(UnsupportedFileType):
        inspect_docx(bytes(data))


def test_the_entry_read_is_bounded_even_when_the_header_under_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The byte counter is the check that matters: a header's declared size is not trusted."""
    monkeypatch.setattr(detect, "MAX_ZIP_ENTRY_BYTES", 1000)
    buffer = io.BytesIO(_docx({"word/document.xml": b"z" * 5000}))
    with zipfile.ZipFile(buffer) as archive:
        with pytest.raises(UnsupportedFileType):
            detect._read_bounded(archive, "word/document.xml")


@pytest.mark.parametrize(
    ("data", "name"),
    [
        ("Plain UTF-8 text: cafÃ©".encode(), "notes.txt"),
        (b"# Titre\n\nUne note.", "notes.md"),
    ],
    ids=["txt", "md"],
)
def test_utf8_text_is_recognised_for_its_extension(data: bytes, name: str) -> None:
    assert detect_kind(data, name) in (Kind.TXT, Kind.MD)


def test_text_that_is_not_utf8_is_refused() -> None:
    with pytest.raises(UnsupportedFileType):
        detect_kind(b"caf\xe9 in latin-1", "notes.txt")


def test_text_with_a_nul_byte_is_refused() -> None:
    with pytest.raises(UnsupportedFileType):
        detect_kind(b"hello\x00world", "notes.md")


def test_an_extension_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(UnsupportedFileType):
        detect_kind(b"hello", "script.exe")


def test_a_file_without_an_extension_is_refused() -> None:
    with pytest.raises(UnsupportedFileType):
        detect_kind(b"hello", "README")


# ---- the streaming body cap (ASGI, no database) -----------------------------------------------


async def _app_reading_everything(scope: Any, receive: Any, send: Any) -> None:
    total = 0
    while True:
        message = await receive()
        total += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(total).encode()})


def _scope(headers: Iterable[tuple[bytes, bytes]] = ()) -> MutableMapping[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/documents",
        "headers": list(headers),
    }


async def _chunks(parts: list[bytes]) -> AsyncIterator[MutableMapping[str, Any]]:
    for index, part in enumerate(parts):
        yield {"type": "http.request", "body": part, "more_body": index < len(parts) - 1}


async def _run(
    app: UploadBodyLimit, scope: MutableMapping[str, Any], parts: list[bytes]
) -> list[MutableMapping[str, Any]]:
    sent: list[MutableMapping[str, Any]] = []
    chunks = _chunks(parts)

    async def receive() -> MutableMapping[str, Any]:
        return await chunks.__anext__()

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _status(sent: list[MutableMapping[str, Any]]) -> int:
    start = next(message for message in sent if message["type"] == "http.response.start")
    return int(start["status"])


@pytest.mark.anyio
async def test_a_declared_length_over_the_cap_is_refused_without_reading_the_body() -> None:
    app = UploadBodyLimit(_app_reading_everything, path="/documents", max_bytes=100)
    scope = _scope([(b"content-length", b"101")])
    sent = await _run(app, scope, [b"never read"])
    assert _status(sent) == 413


@pytest.mark.anyio
async def test_a_chunked_body_over_the_cap_is_refused_without_a_content_length() -> None:
    """No Content-Length: the cap is still enforced while the chunks stream in."""
    app = UploadBodyLimit(_app_reading_everything, path="/documents", max_bytes=100)
    sent = await _run(app, _scope(), [b"a" * 60, b"b" * 60])
    assert _status(sent) == 413


@pytest.mark.anyio
async def test_a_body_under_the_cap_reaches_the_app_unchanged() -> None:
    app = UploadBodyLimit(_app_reading_everything, path="/documents", max_bytes=100)
    sent = await _run(app, _scope([(b"content-length", b"10")]), [b"a" * 5, b"b" * 5])
    assert _status(sent) == 200
    assert sent[-1]["body"] == b"10"


@pytest.mark.anyio
async def test_other_paths_are_not_limited() -> None:
    app = UploadBodyLimit(_app_reading_everything, path="/documents", max_bytes=10)
    scope = {**_scope(), "path": "/auth/login"}
    sent = await _run(app, scope, [b"x" * 500])
    assert _status(sent) == 200

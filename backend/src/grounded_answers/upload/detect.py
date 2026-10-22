"""
Detect the type of an upload from its bytes (SPEC §6.1, ARCHITECTURE §3, ADR 0019 decision D3).

The client's Content-Type header is never read. The file's content decides the type, and the
extension must agree with it. Refusals raise UnsupportedFileType, which the endpoint turns into 415.

- PDF: starts with "%PDF-", and the extension is .pdf.
- DOCX: a ZIP archive that holds [Content_Types].xml and word/document.xml, inspected without
  extracting anything to disk, within entry-count and size limits. Extension .docx.
- TXT and MD: valid UTF-8 with no NUL byte, and not the magic of another type. Extension .txt or .md.
"""

import io
import zipfile
import zlib
from enum import StrEnum
from typing import Final

from grounded_answers.upload.limits import (
    MAX_ZIP_ENTRIES,
    MAX_ZIP_ENTRY_BYTES,
    MAX_ZIP_TOTAL_BYTES,
)

PDF_MAGIC: Final = b"%PDF-"
ZIP_MAGIC: Final = b"PK\x03\x04"
DOCX_REQUIRED_ENTRIES: Final = frozenset({"[Content_Types].xml", "word/document.xml"})
_ZIP_ENCRYPTED_FLAG: Final = 0x1
_READ_CHUNK: Final = 64 * 1024


class UnsupportedFileType(Exception):
    """The bytes or the extension are not an allowed type. The response is 415, with no detail."""


class Kind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    MD = "md"


# The label stored with the version. It comes from the detected kind, never from the client.
CONTENT_TYPE: Final[dict[Kind, str]] = {
    Kind.PDF: "application/pdf",
    Kind.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    Kind.TXT: "text/plain",
    Kind.MD: "text/markdown",
}

_EXTENSION_KIND: Final[dict[str, Kind]] = {
    ".pdf": Kind.PDF,
    ".docx": Kind.DOCX,
    ".txt": Kind.TXT,
    ".md": Kind.MD,
}


def detect_kind(data: bytes, filename: str) -> Kind:
    """Return the kind the bytes show, if the filename's extension agrees. Otherwise refuse."""
    dot = filename.rfind(".")
    extension = filename[dot:].lower() if dot >= 0 else ""
    claimed = _EXTENSION_KIND.get(extension)
    if claimed is None:
        raise UnsupportedFileType("the extension is not allowed")
    if claimed is Kind.PDF:
        if data.startswith(PDF_MAGIC):
            return Kind.PDF
    elif claimed is Kind.DOCX:
        if data.startswith(ZIP_MAGIC):
            inspect_docx(data)
            return Kind.DOCX
    elif _is_plain_text(data):
        return claimed
    raise UnsupportedFileType("the content does not match the extension")


def _is_plain_text(data: bytes) -> bool:
    """Valid UTF-8, no NUL, and not the magic of a binary type (a renamed PDF or DOCX is text)."""
    if data.startswith((PDF_MAGIC, ZIP_MAGIC)) or b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def inspect_docx(data: bytes) -> None:
    """
    Check that the bytes are a DOCX, without extracting anything. Raises UnsupportedFileType.

    Entry count, declared total and declared entry size are checked from the central directory.
    The one entry read is read through a byte counter, because a header can declare a small size
    for a large stream.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnsupportedFileType("not a ZIP archive") from exc
    try:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise UnsupportedFileType("too many archive entries")
        if sum(info.file_size for info in infos) > MAX_ZIP_TOTAL_BYTES:
            raise UnsupportedFileType("the archive is too large when unpacked")
        if any(info.file_size > MAX_ZIP_ENTRY_BYTES for info in infos):
            raise UnsupportedFileType("an archive entry is too large when unpacked")
        if any(info.flag_bits & _ZIP_ENCRYPTED_FLAG for info in infos):
            raise UnsupportedFileType("encrypted archive entries are not allowed")
        names = {info.filename for info in infos}
        if not DOCX_REQUIRED_ENTRIES <= names:
            raise UnsupportedFileType("the archive is not a DOCX")
        _read_bounded(archive, "word/document.xml")
    except (zipfile.BadZipFile, NotImplementedError, zlib.error) as exc:
        raise UnsupportedFileType("the archive cannot be read") from exc
    finally:
        archive.close()


def _read_bounded(archive: zipfile.ZipFile, name: str) -> None:
    total = 0
    with archive.open(name) as entry:
        while chunk := entry.read(_READ_CHUNK):
            total += len(chunk)
            if total > MAX_ZIP_ENTRY_BYTES:
                raise UnsupportedFileType("an archive entry is too large when unpacked")

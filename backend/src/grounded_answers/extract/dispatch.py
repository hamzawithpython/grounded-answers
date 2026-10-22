"""Route a file's kind to its extractor. Runs inside the child process only (ADR 0020)."""

from grounded_answers.extract.docx import extract_docx
from grounded_answers.extract.markdown import extract_markdown, extract_text_file
from grounded_answers.extract.model import Extracted
from grounded_answers.extract.pdf import extract_pdf


def extract_bytes(kind: str, data: bytes) -> Extracted:
    if kind == "pdf":
        return extract_pdf(data)
    if kind == "docx":
        return extract_docx(data)
    if kind == "md":
        return extract_markdown(data.decode("utf-8"))
    if kind == "txt":
        return extract_text_file(data.decode("utf-8"))
    raise ValueError(f"no extractor for kind {kind!r}")

"""
Load the owner's evaluation questions and turn each answerable one into expected (document, clause) pairs.

The file is demo-data/indus-freight/eval-questions.json. A question names its source in one of three shapes:
- one document and one clause: "3.2.2";
- one document and several clauses, each with an optional version: "1.5.1 (v2.0); 6.1 (v1.0)";
- several documents and as many clauses, matched by position.

A pair with no version means the document's current version. Unanswerable questions have no pairs.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

UNANSWERABLE: Final = "unanswerable"
_CLAUSE = re.compile(r"^(?P<clause>\d+(?:\.\d+)*)(?: \(v(?P<version>\d+\.\d+)\))?$")


class QuestionFileError(ValueError):
    """The question file does not have the shape this module expects."""


@dataclass(frozen=True)
class ExpectedPair:
    document: str
    clause: str
    # None means the document's current version.
    version: str | None


@dataclass(frozen=True)
class EvalQuestion:
    id: str
    text: str
    type: str
    pairs: tuple[ExpectedPair, ...]

    @property
    def answerable(self) -> bool:
        return self.type != UNANSWERABLE


def _pair(document: str, text: str, question_id: str) -> ExpectedPair:
    match = _CLAUSE.match(text.strip())
    if match is None:
        raise QuestionFileError(f"{question_id}: cannot read the clause {text!r}")
    return ExpectedPair(document, match["clause"], match["version"])


def _pairs(raw: dict[str, Any]) -> tuple[ExpectedPair, ...]:
    question_id = str(raw["id"])
    documents, sections = raw["source_document"], raw["source_section"]
    if isinstance(documents, str) and isinstance(sections, str):
        return tuple(_pair(documents, part, question_id) for part in sections.split(";"))
    if (
        isinstance(documents, list)
        and isinstance(sections, list)
        and len(documents) == len(sections)
    ):
        return tuple(
            _pair(str(document), str(section), question_id)
            for document, section in zip(documents, sections, strict=True)
        )
    raise QuestionFileError(f"{question_id}: source_document and source_section do not match")


def load_questions(path: Path) -> list[EvalQuestion]:
    raw_questions = json.loads(path.read_text(encoding="utf-8"))
    questions: list[EvalQuestion] = []
    for raw in raw_questions:
        kind = str(raw["type"])
        pairs = () if kind == UNANSWERABLE else _pairs(raw)
        if kind != UNANSWERABLE and not pairs:
            raise QuestionFileError(f"{raw['id']}: an answerable question needs a source")
        questions.append(EvalQuestion(str(raw["id"]), str(raw["question"]), kind, pairs))
    return questions

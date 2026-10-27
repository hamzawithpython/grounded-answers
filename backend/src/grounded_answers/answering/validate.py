"""
Checking a model's answer against the sources it was given (ADR 0032, decision 2).

Quote validation proves that the cited text EXISTS in the cited source. It does not prove that the whole answer is
supported by it: that is measured in Step 7 (answer groundedness). The numeric check below catches one common way an
answer drifts from its quote, a number that the quote does not contain.

Rules for a model answer that does not refuse:
- the answer text is not empty, and there is at least one citation;
- every ref is one of the provided sources (1 to k);
- every quote has no ellipsis, is not longer than 500 normalised characters, and appears in the cited source after
  normalisation (normalise.py). A text quote needs at least 20 normalised characters. A table quote needs at least 8 and
  at least one word with a letter, so a row label and a value ("g3 2,500") pass and bare numbers do not;
- every number in the answer text (digits only; clause labels and [n] refs ignored) appears in at least one quote;
- a [n] marker in the answer text names one of the provided sources (1 to k);
- the answer text is not mostly non-Latin script (rule 3 says English; Roman Urdu and other Latin-script text pass, and
  quotes are never checked, since Urdu documents will make Urdu quotes legitimate; v2 Urdu answers replace this rule);
- the answer text does not repeat the instructions: no run of 8 words from the system rules (prompt echo).

A quote the model copied from the escaped text it was shown (`&lt;source` for `<source`) is accepted too (Step 6).

Any failure invalidates the whole answer (strict rule, option 2A). The caller retries once and then refuses. The result
is a code, never text from the answer or the quote, so that it is safe to log.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from grounded_answers.answering.clauses import ClauseLabel, clause_at, is_table_chunk
from grounded_answers.answering.normalise import normalise
from grounded_answers.answering.prompt import (
    CORRECTIVE_NOTE,
    REMINDER,
    SYSTEM_INSTRUCTIONS,
    unescape_tags,
)
from grounded_answers.answering.text import is_mostly_non_latin
from grounded_answers.answering.types import ModelAnswer
from grounded_answers.retrieval import RetrievedChunk

MIN_TEXT_QUOTE: Final = 20
MIN_TABLE_QUOTE: Final = 8
MAX_QUOTE: Final = 500

_NUMBER: Final = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_REF: Final = re.compile(r"\[\d+\]")
# A marker the model may put in the answer text. One to three digits, so "[2024]" is not taken for a source.
_MARKER: Final = re.compile(r"\[(\d{1,3})\]")
# Prompt echo: this many words in a row from the rules, in the answer.
ECHO_WORDS: Final = 8
_WORD: Final = re.compile(r"\w+")


def _grams(text: str) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.casefold())
    return {tuple(words[i : i + ECHO_WORDS]) for i in range(len(words) - ECHO_WORDS + 1)}


_RULE_GRAMS: Final = _grams(SYSTEM_INSTRUCTIONS) | _grams(REMINDER) | _grams(CORRECTIVE_NOTE)


def echoes_the_prompt(answer: str) -> bool:
    return not _grams(answer).isdisjoint(_RULE_GRAMS)


_THREE_PART_LABEL: Final = re.compile(r"\b\d+(?:\.\d+){2,}\b")
_NAMED_LABEL: Final = re.compile(
    r"(?:clauses?|sections?|§|paragraphs?|para|points?|tables?|articles?|annex|appendix|rules?)\s*\d+(?:\.\d+)*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ValidCitation:
    ref: int
    quote: str
    chunk_index: int
    location: ClauseLabel


@dataclass(frozen=True)
class Valid:
    citations: tuple[ValidCitation, ...]


@dataclass(frozen=True)
class Invalid:
    # One of: empty_answer, no_citations, bad_ref, quote_ellipsis, quote_too_short, quote_too_long,
    # quote_not_informative, quote_not_found, number_not_grounded, answer_language, prompt_echo.
    code: str


def numbers_in(text: str, *, drop_labels: bool) -> set[str]:
    """The numbers in `text` in one form: thousands commas removed. With `drop_labels`, [n] refs, clause labels
    with two or more dots, and numbers named as a clause, section or table ("section 3.2", "Table 4.1") are ignored."""
    if drop_labels:
        text = _NAMED_LABEL.sub(" ", _THREE_PART_LABEL.sub(" ", _REF.sub(" ", text)))
    return {m.group(0).replace(",", "") for m in _NUMBER.finditer(text)}


def _has_a_word(quote: str) -> bool:
    return any(any(ch.isalpha() for ch in token) for token in quote.split())


def validate_answer(answer: ModelAnswer, chunks: Sequence[RetrievedChunk]) -> Valid | Invalid:
    """Validate a non-refusing answer against the chunks it was given, in order (ref n is chunks[n - 1])."""
    if not answer.answer.strip():
        return Invalid("empty_answer")
    if not answer.citations:
        return Invalid("no_citations")
    if is_mostly_non_latin(answer.answer):
        return Invalid("answer_language")
    if echoes_the_prompt(answer.answer):
        return Invalid("prompt_echo")
    valid: list[ValidCitation] = []
    seen: set[tuple[int, str]] = set()
    for citation in answer.citations:
        if not 1 <= citation.ref <= len(chunks):
            return Invalid("bad_ref")
        if "..." in citation.quote or "…" in citation.quote:
            return Invalid("quote_ellipsis")
        chunk = chunks[citation.ref - 1]
        table = is_table_chunk(chunk.body)
        quote, _ = normalise(citation.quote)
        if len(quote) < (MIN_TABLE_QUOTE if table else MIN_TEXT_QUOTE):
            return Invalid("quote_too_short")
        if len(quote) > MAX_QUOTE:
            return Invalid("quote_too_long")
        if table and not _has_a_word(quote):
            return Invalid("quote_not_informative")
        body, offsets = normalise(chunk.body)
        at = body.find(quote)
        if at < 0:
            # The model saw the escaped text: `&lt;source` where the document has `<source`. Accept that too.
            unescaped = normalise(unescape_tags(citation.quote))[0]
            at = body.find(unescaped) if unescaped != quote else -1
            quote = unescaped
        if at < 0:
            return Invalid("quote_not_found")
        if (citation.ref, quote) in seen:
            continue
        seen.add((citation.ref, quote))
        location = clause_at(chunk.body, chunk.section_number, offsets[at])
        valid.append(
            ValidCitation(citation.ref, citation.quote.strip(), citation.ref - 1, location)
        )
    if any(not 1 <= int(mark) <= len(chunks) for mark in _MARKER.findall(answer.answer)):
        return Invalid("bad_ref")
    quoted: set[str] = set()
    for cited in valid:
        quoted |= numbers_in(normalise(cited.quote)[0], drop_labels=False)
    if not numbers_in(answer.answer, drop_labels=True) <= quoted:
        return Invalid("number_not_grounded")
    return Valid(tuple(valid))

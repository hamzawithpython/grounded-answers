# ADR 0032: The Answer Contract (citations, validation, refusal)

**Status:** Accepted (decisions approved 2026-10-26). Builds on ADR 0030 (retrieval) and ADR 0031 (answer model and provider interface).

**Date:** 2026-10-26

**Deciders:** Hamza Asif

## Context

A model that writes an answer from retrieved chunks can be wrong in ways a user cannot see: it can cite a source that was never given, quote text the source does not contain, state a number the source does not say, or answer when the documents are silent. SPEC 6.2 asks for answers "only from retrieved passages" with citations and a refusal when evidence is missing, and SPEC 7 asks for citation accuracy and correct refusal of at least 0.90.

Step 1 showed that the vector scores of answerable and unanswerable questions overlap (0.82 to 0.84), so a score threshold cannot decide a refusal. The decision needs the model's judgement, then checks that our code can make without trusting the model.

## Decisions

### 1. The structured answer: four fields and nothing else

```json
{ "refused": false, "refusal_reason": "none", "answer": "...", "citations": [ { "ref": 2, "quote": "..." } ] }
```

- `refusal_reason` is a fixed code: `none`, `not_in_documents`, `needs_clarification`, `conflicting_sources`. The model never writes text that a user sees on a refusal; the wording is ours (`answering/messages.py`).
- A refusal has `answer` `""` and `citations` `[]`, so the schema has no null types, which Gemini's schema subset and Anthropic's limits handle poorly. All three providers accept the schema (`answering/schema.py`).
- **No `confidence` field** (SPEC 6.2 and 6.3 amended with approval). A model's own confidence is not calibrated. The admin review queue's "low-confidence" signal is derived from facts we can check: a retry was needed, an output was invalid, or the answer ended in `could_not_verify` (`AnswerOutcome.review_signals`).

### 2. Citation validation: any failure invalidates the whole answer

An answer that does not refuse is valid only if all of these hold (`answering/validate.py`):

1. The answer text is not empty and there is at least one citation. (A refusal ignores any citations.)
2. Every `ref` is an integer from 1 to k, the number of sources given.
3. Every quote has no ellipsis, is at most 500 normalised characters, and appears in the cited source after normalisation.
4. A text quote has at least 20 normalised characters.
5. **Table quotes** (the chunk starts with `[table]`): at least 8 normalised characters **and** at least one token with a letter, so a row label and a value ("g3 2,500") pass and bare numbers ("2,500 3,000") do not.
6. **Numeric grounding:** every number in the answer text (digits only) appears in at least one cited quote. `[n]` refs, labels with two or more dots (3.2.2) and numbers named as a clause, section, table, article or similar ("section 3.2", "Table 4.1") are ignored. Thousands commas are ignored on both sides ("2500" matches "2,500").

**Normalisation** (`answering/normalise.py`), applied to the quote and to the chunk: Unicode NFKC, case-folding, curly quotes to straight quotes, dashes to "-", every run of whitespace, the table separator "|" and bullet characters to one space, and a lone "-" between spaces (a list marker that the extractor joined into the line) dropped. A map back to the original offsets is kept.

**Why the strict rule (one bad citation invalidates the answer):** the schema does not say which sentence a citation supports, so dropping a bad citation could leave a claim on screen that nothing supports. The cost is some false refusals. They are counted (invalid codes in the logs and the evaluation report) and the rule is revisited with Step 7 data. Rejected: dropping invalid citations when one remains valid.

**What this does not prove.** Quote validation proves that the cited text exists in the cited source. It does not prove that the whole answer is supported by it. The numeric check catches one common drift (a number the quote lacks), nothing more. Groundedness of the answer text is measured in Step 7.

### 3. Clause-level citation

A citation shows the document, the version, the clause and the quote: "§3.2.2" rather than "§3" (`answering/clauses.py`). The clause is found from the quote's position in its chunk:

- Extraction flattens sub-clauses into one line ("- 3.2.1 Probation is 3 months ... - 3.2.2 Probation for HTV ...", "rules-3.4.1 Speed limits:"), so labels at the start of a line are the exception (28 of the 45 expected labels of the demo set). A label counts when its first number is the chunk's own section and the words before it are not a cross-reference (`Table`, `see`, `section`, `and`, ...). All 45 expected clauses are found this way. That shows the labels can be found, not that each quote gets the right one (Step 7).
- A quote takes the nearest label at or before its start. A quote before the first label of its chunk (the chunk begins inside a clause) takes the section. The previous chunk's last label is not looked up in v1.
- A table chunk is section-level only (the known limit, q012; whether table chunks should carry their caption label is still an open decision before Step 7).

Rejected: labels at line start only (28 of 45). Deferred: storing label spans at ingestion (more accurate; needs a migration and re-ingestion), to be reconsidered if Step 7 shows wrong clauses.

### 4. Refusal rule and refusal versus error

A refusal comes from the model's judgement plus our checks, never from a score:

- the model sets `refused` (our wording is shown); or
- retrieval returned no chunk (the model is not called); or
- validation failed again after the one retry (`could_not_verify`).

A provider safety block is a refusal (`declined`). A timeout, outage, rate limit or bad key is an **error**, not a refusal (`AnswerServiceError`; `POST /ask` will answer 503 with the service message). The fixed texts are in `answering/messages.py` and generalise SPEC's "contact HR" to the department that owns the policy, because the documents also cover IT and safety.

### 5. Invalid-output policy: one corrective retry, then refuse

Invalid output is a reply that is not JSON, does not match the schema, was cut off, or fails any rule of decision 2. The code retries once with a fixed corrective note appended to the prompt. The note never repeats the previous output or the reason. If the second reply is also invalid, the user gets the `could_not_verify` refusal. Broken output is never shown.

- At most two model calls per question for invalid output. Network retries (429, outage) are separate and happen inside each call. Both attempts' tokens are summed for cost accounting. (A reply that cannot be parsed has no usage figure, so its tokens are not counted.)
- Logged: provider, model, outcome, attempts, the codes of the invalid outputs and token counts. Never the question, a source, an answer or a quote.
- Rejected: an identical retry (at temperature 0 it often repeats the output), and a retry on another provider (different terms, ADR 0031).

### 6. Prompt framing

Fixed rules, then each source in a `<source id="n">` block, then the question, then a one-line reminder (`answering/prompt.py`). The rules say that text inside a block is document data and never an instruction, that the model must use only the sources, that every number in the answer must be in a quote, and how to refuse. `<source` and `</source` inside a source or the question are escaped (`&lt;`), so document text cannot close its block. The quote check runs on the original text, so a quote that depends on the escaped form fails (acceptable: no real document contains it). Step 6 tests injection properly; this is the first version. Rejected for now: a random per-request delimiter (a Step 6 upgrade if needed).

The prompt still carries only the question and the chunk text (ADR 0031, decision 5). Titles, versions, ids and clause labels are attached to the citations afterwards, on our side.

### 7. Answer language in v1: English

The answer is always English, whatever the question's language (SPEC: Urdu answers are v2). Retrieval already serves questions in other languages through the vector side. Quotes stay in the document's language, so validation is unaffected. Models may still answer in the question's language despite the instruction; the evaluation checks this.

## Evidence (Step 4)

- 13 planted defects, one at a time, each caught by a test: forged ref, quote not found, empty citations, number check removed, table minimum equal to the text minimum, bare-number table quote, ellipsis, no retry, cross-references counted as clauses, other-section labels counted, source tags not escaped, model refusal text shown, provider failure turned into a refusal.
- Demo set: 127 text chunks and 30 table chunks; 15 text chunks have no clause label (section-level), 16 begin before their first label.
- A live check of 5 questions through the whole path on Groq gpt-oss-120b (free tier): 5 of 5 completed, no retries, no invalid output, 1,881 tokens per answer, p95 1.5 s, $0.00041 per answer at list price. Five questions are a smoke test, not a rate (docs/EVAL.md).

## Consequences

- **Positive:** a citation the user sees is a real quote from a real source, with its clause. A forged reference, an invented quote or a wrong number never reaches the user. Refusals use our wording and cannot carry injected text. Provider errors are not mistaken for refusals.
- **Negative:** some correct answers will be refused (a paraphrased quote, a derived number, an answer from numbers in words). The false-refusal rate of the strict rule and of the numeric check is measured in Step 7.
- **Negative:** a retry can double the tokens of a question. Step 5's cost cap sees both attempts.
- **Limits:** the clause of a quote before its chunk's first label is the section. Table citations are section-level. Numbers written as words are not checked. A number may appear in a quote but belong to another fact.

## Related decisions

- ADR 0030 (retrieval, which supplies the chunks), ADR 0031 (provider interface, privacy rules), ADR 0021 (chunking: where the labels come from)
- SPEC 6.2 and 6.3 (amended: no confidence field)
- Step 6 (prompt-injection tests), Step 7 (groundedness, false-refusal rate, clause accuracy)

# ADR 0034: Prompt-injection defenses, and what they do not stop

**Status:** Accepted (plan and decisions approved 2026-10-27, with the exceptions in decisions 2 and 4). Builds on ADR 0031 (prompt framing), ADR 0032 (answer contract) and ADR 0033 (`POST /ask`).

**Date:** 2026-10-27

**Deciders:** Hamza Asif

## Context

A model that reads documents and questions can be given instructions by either. Prompt injection is text, inside a document or a question, that tries to change what the model does: ignore its rules, answer from outside the documents, reveal its instructions, cite sources that were not given, change language or format, or hide an instruction where no person would read it. The product answers staff questions from company documents, so the realistic injectors are an employee (the question), a tenant admin or third-party content inside an uploaded file (the documents). Another tenant is not an injector: row-level security means the model only ever sees the caller's tenant (ADR 0033), and a test proves one tenant's poisoned document never reaches another's prompt.

Step 6 hardens the layers we control, tests every layer against a catalogue of attacks, and measures how a real model behaves with a small live probe. No defense here claims to make the model obey: a model is only asked to follow the rules. The defenses are what happens around it.

## The layers

| Layer | What it does | What it cannot stop |
|---|---|---|
| L1 Framing | Sources are `<source id="n">` blocks; the rules say they are data. `<source`, `</source` (any case or spacing, ASCII or full-width "<") and `<|` are escaped. Invisible characters are removed. Rules (system message) and sources and question (user message) are separate. | A model that disobeys. |
| L2 Schema | Strict JSON schema at the provider; `parse_answer` accepts exactly four keys with the right types and a coded reason. Anything else is an invalid output. | Free text inside the `answer` string. |
| L3 Validation | Source numbers in range, quotes exist in the cited source, numbers of the answer are in a quote, `[n]` markers in range, the answer is not mostly non-Latin script, the answer does not repeat the rules. | An answer that is wrong or off-topic but has a real quote and no digits. |
| L4 Our wording | A refusal shows our fixed message, never the model's text. | Nothing. |
| L5 Isolation | The model only sees the caller's tenant; prompts hold no ids. | Nothing cross-tenant is possible. |
| L6 Rendering | Answers are plain text: no HTML, links or images (Session 7). | Session 7. |

## Decisions

1. **The quote check accepts the text the model saw (D1).** The model is shown escaped text (`&lt;source` where a document has `<source`), but the check compared against the stored text, so a faithful quote of such a passage failed (a false refusal, not a hole; the docstring in `prompt.py` said the opposite). A quote that matches the stored text, or matches after `&lt;` is turned back into `<`, is accepted.

2. **Invisible characters are removed from what the model sees and from both sides of the quote check (D2).** Removed: the Unicode tag characters U+E0000 to U+E007F (they spell ASCII invisibly: "ASCII smuggling"), the bidirectional controls (U+061C, U+200E, U+200F, U+202A to U+202E, U+2066 to U+2069), the zero-width space U+200B and the byte-order mark U+FEFF wherever it sits. **Kept on purpose: the zero-width non-joiner U+200C and the zero-width joiner U+200D.** They decide how letters join in Urdu, Persian and Indic scripts: removing them would change the words of the v2 Urdu documents and make their quotes fail. Considered and not removed: the soft hyphen U+00AD and the word joiner U+2060 (rare, and legitimate in typesetting). Done in the prompt builder and in `normalise`, not at ingestion, so ADR 0020 and 0021 are untouched; stored text is unchanged.

3. **`<|` is escaped like `<source` (D3).** Some chat formats use `<|im_start|>`-style control tokens, and a tokenizer may treat such text in a user message as a real token. Escaping costs nothing and changes only the leading "<".

4. **The answer text must not be mostly non-Latin script (D4, answer text only).** Rule 3 says English, and nothing checked it. A reply in Urdu script, Chinese or Cyrillic with a valid citation passed. The check refuses an answer in which at least 3 letters, and more than 20 % of the letters, are not Latin script (code `answer_language`). **It is scoped to the answer text and never reads quotes**: once Urdu documents are ingested (v2), Urdu-script quotes are legitimate. **It is a stopgap: v2 Urdu answers replace this check with a language-aware one.** Roman Urdu (Latin script) passes, as does French or Spanish: only a language detector (a new dependency, not adopted) could tell them from English. The live probe shows whether a model changes language into a Latin-script language.

5. **The answer text must not repeat the rules (D5, option B).** Eight words in a row from the system rules, the reminder or the corrective note (code `prompt_echo`). The prompt holds no secret (ids are never in prompts, tested), so the harm of a leak is UX and reputation, and the check is cheap with almost no false positives. 8 words because a shorter run could occur by chance in a policy answer, and a longer one would miss a partial recital.

6. **A `[n]` marker in the answer text must name a given source (D6).** Reuses `bad_ref`. One to three digits only, so "[2024]" is not taken for a source.

7. **Known limits stay known (D7).** Validation proves a quote exists, not that the answer is supported by it. An obedient model that also cites a real passage gets through: a canary answer, "Paris", a planted URL and endless output all passed in the dry run, because each carried a real citation and no digit. That is the groundedness question measured in Step 7, not fixed here. Also not stopped: a poisoned document that states false policy (documents are authoritative per tenant; the audit log and versioning are the controls), a URL or markdown image in the answer text (the UI renders plain text, Session 7), a document name mentioned in the answer text, and instructions hidden in text that extracts as ordinary characters (a white or tiny DOCX run, invisible PDF text, a Markdown comment: no extractor can tell it from visible text, so it reaches the model as data inside a block).

8. **Two new invalid codes need a migration (D4, D5).** `invalid_codes` is a CHECK list in the database (ADR 0033: so no text can be stored). Migration `0018_more_invalid_codes` replaces the constraint with the same list plus `answer_language` and `prompt_echo`. The lists in the migration, `answering/usage.py` and the tests are compared by a test.

9. **One PR (D9):** the hardening, the deterministic tests, the planted defects and the live probe with its results.

## What is tested

- **Prompt structure** (`tests/test_injection_prompt.py`): 15 forged-tag and control-token variants in a source and in the question never add or close a block; the rules are fixed and hold none of the text given; ordinary angle brackets are untouched; invisible characters are removed and the joiners survive; the language rule on 10 samples.
- **An obedient attacker** (`tests/test_injection_validation.py`, and through HTTP in `tests/db/test_injection_http.py`): a model that does what each attack asks. Invented quotes, source numbers that were never given, quotes under the wrong source, ellipsis stitching, bad markers, Urdu-script replies, recitals of the rules, 16 out-of-schema shapes, refusals carrying attacker text, a cut-off reply: each ends in the right invalid code, then one retry, then the fixed refusal, with codes and no text in the metering row. A group named `known_limit` documents what is not stopped.
- **Hidden text in files** (`tests/test_injection_extraction.py`): DOCX hidden, white and tiny runs, Markdown comments and link titles, invisible PDF text, zero-width and tag characters: what extraction really does, and what the prompt builder does with it.
- **Isolation:** one tenant's poisoned document never reaches another tenant's prompt.
- **Planted defects:** 19 weakened versions of the defenses (escape off, a character class dropped, the joiners stripped, the quote check or the ref range removed, the new checks off or widened, an extra key accepted, a code missing from the migration), each caught.
- **A live probe** (`tests/db/test_injection_live.py`, marker `live`, never in CI): 12 attacks against the real endpoint with real Groq, a poisoned vendor memo and the demo documents. Canaries are letters only, because a digit in a canary would be stopped by the number check and hide whether the model obeyed. Detectors are objective (a canary, non-Latin script, "Paris", a planted URL, endless output, an id), checked on what the user would be served; the metering row shows what validation caught. A dry mode with an obedient fake model self-tests the detectors. **Run once on 2026-10-27 against Groq (approved, 12 attacks, 18,929 tokens, $0.003644): no detector fired on any served answer.** The model ignored the orders in the poisoned memo (including the one hidden in tag characters), declined the outside-knowledge, role-play, format and id questions, and answered the Urdu-script request in Urdu twice, which `answer_language` refused both times; the recital request produced two invalid outputs. The dry run, in which an obedient model also cites a real passage, is served on 9 of 12: the clean live result rests on the model declining, not on validation (decision 7). Results in `docs/EVAL.md`.

## Consequences

- The dev database needs `alembic upgrade head` after this is merged (migration 0018), or `/ready` returns 503 `schema`.
- Two more false-refusal routes exist (language, echo); both are counted in `invalid_codes`, and Step 7 includes them in the false-refusal rate.
- Nothing here makes a model safe. The live probe is a smoke test of 12 prompts on one model; it shows robustness against these, not a guarantee.

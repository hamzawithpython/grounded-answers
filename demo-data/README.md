# Demo data: Indus Freight Lines (fictional)

**Everything in this folder is fictional.** The company "Indus Freight Lines", its people, its email addresses, its policies and its procedures were written for software demonstration. None of it describes a real company, a real person, or a real policy. The email domain `indusfreight.example` is reserved for examples and does not receive mail. No file contains a credential, a key, or a real identifier.

It is used by the end-to-end run in `docs/runbooks/step-8-end-to-end.md`, and by the retrieval evaluation (`docs/EVAL.md`).

## Layout

```text
demo-data/indus-freight/
  source/           the Markdown text of each document (8 files)
  upload/           the same documents as uploaded files (8 files: .docx, .pdf, .md)
  eval-questions.json   50 evaluation questions (40 answerable, 10 unanswerable)
```

## The eight documents

The ID is the code in the file name. The upload set has the seven current documents and Expense Policy v1.0. Expense Policy v2.0 is the replacement that the runbook uploads in step 7.

| # | ID | Title | Upload file | Format | Role in the run |
|---|---|---|---|---|---|
| 1 | IFL-FIN-007 | Expense Policy, v1.0 | `IFL-FIN-007_expense-policy_v1.0.docx` | DOCX | uploaded, then replaced |
| 2 | IFL-FIN-007 | Expense Policy, v2.0 | `IFL-FIN-007_expense-policy_v2.0.docx` | DOCX | the replacement (step 7) |
| 3 | IFL-HR-001 | HR Leave Policy, v3.0 | `IFL-HR-001_hr-leave-policy_v3.0.docx` | DOCX | uploaded |
| 4 | IFL-HR-002 | Code of Conduct | `IFL-HR-002_code-of-conduct.docx` | DOCX | uploaded |
| 5 | IFL-IT-006 | IT Acceptable Use | `IFL-IT-006_it-acceptable-use.md` | Markdown | uploaded |
| 6 | IFL-OPS-003 | Driver Handbook | `IFL-OPS-003_driver-handbook.pdf` | PDF (text layer) | uploaded |
| 7 | IFL-SAF-004 | Warehouse Safety | `IFL-SAF-004_warehouse-safety.pdf` | PDF (text layer) | uploaded |
| 8 | IFL-SAF-005 | Accident Reporting | `IFL-SAF-005_accident-reporting.pdf` | PDF (text layer) | uploaded |

The `source/` folder has the Markdown text for all eight, including both expense versions, under the names `expense-policy-v1.md`, `expense-policy-v2.md`, `hr-leave-policy.md`, `code-of-conduct.md`, `it-acceptable-use.md`, `driver-handbook.md`, `warehouse-safety.md` and `accident-reporting.md`.

The three PDFs have an extractable text layer. They are not scanned images, so they are not expected to fail with `no_text`.

## The evaluation questions

`eval-questions.json` holds 50 questions, each with an expected answer and a type:

- **40 answerable**, from the documents above (types `fact`, `table`, `conditional`, `version`, `cross_document`).
- **10 unanswerable** (type `unanswerable`). The correct behaviour is to say the documents do not answer the question. The answers are not in the set.

`docs/EVAL.md` describes the evaluation over this file. No automated test reads the file yet, so the counts above were checked by counting the file's entries, and no test keeps them fixed.

## What not to do

- Do not replace these with real company documents. If you need real data, use a separate, private folder outside this repository.
- Do not add real people, real addresses, real phone numbers or real identifiers, even as examples.

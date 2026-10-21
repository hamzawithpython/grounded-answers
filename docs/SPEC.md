# Grounded Answers — Product Specification (v0.1)

> Status: DRAFT — owner: Hamza Asif. Every change to this file is recorded in CHANGELOG.md.
> Decisions that change scope get an ADR in `docs/adr/`.

## 1. Problem

Employees in small and mid-size companies ask the same policy and process questions again and again
("How many casual leaves do I get?", "What is the procedure after a vehicle accident?").
The answers exist, but they are scattered across PDFs, Word files and old emails.
HR and operations staff lose hours every week answering repeat questions, and employees
sometimes act on wrong or outdated information.

## 2. Solution (one sentence)

A multi-tenant assistant that answers staff questions **only from the company's own approved
documents**, shows **citations** for every answer, **refuses** when the documents don't contain
the answer, and gives admins **evaluation and usage dashboards** to prove it works.

## 3. Demo tenant (fictional)

- **Company:** Indus Freight Lines (fictional) — 80-person logistics company (head office, warehouse, drivers).
- **Document set (synthetic, written for this project):**
  - HR & Leave Policy
  - Code of Conduct
  - Driver Handbook (licensing, hours, vehicle checks)
  - Warehouse Safety SOPs
  - Accident & Incident Reporting Procedure
  - IT & Acceptable Use Policy
  - Expense & Travel Reimbursement Policy
- A second fictional tenant (e.g., a small school) is added later **only to test tenant isolation**.

## 4. Users and roles

| Role | Who | Can do |
|---|---|---|
| Platform owner | Hamza (operator) | Create tenants and run evals for any tenant **via an admin CLI only** (uses the migration role; no web access to tenant data). View system health (`/ready`, no tenant data). See ADR 0004. |
| Tenant admin | HR / Ops head | Upload/replace/delete documents, manage users, view analytics and eval results, review flagged answers |
| Employee | Staff member | Ask questions via web chat or Telegram, view citations, give feedback (helpful / not helpful) |

## 5. Channels (v1)

1. **Web chat** (Next.js) — employees log in and chat; citations open the source passage.
2. **Telegram bot** — employees link their Telegram account to their company account once, then ask
   questions in the bot. Same answer engine, same permissions, same logging.
3. **Admin panel** (Next.js) — documents, users, analytics, evaluation dashboard.

No WhatsApp. No phone/voice in v1.

## 6. Functional requirements (v1)

### 6.1 Documents
- Upload PDF, DOCX, TXT, Markdown. Max file size and allowed types enforced server-side.
- Background ingestion: extract text → clean → split into chunks → create embeddings → store.
- Document versioning: replacing a document retires old chunks; answers always cite the current version.
- Ingestion status visible to admins (queued / processing / ready / failed + reason).
- **Deletion:** an admin can delete a document. A deleted document, and any replaced version, is excluded from answers as soon as the deletion is saved. Its content is permanently purged after a retention period (default 30 days, set per deployment). Restore is not supported in v1. (ADR 0007)
- **Replacement:** a new version replaces the current one for answers once ingestion succeeds. The previous version follows the same retention period.
- **Upload limits:** maximum 10 MiB per file. The file type is checked from the file's content, not the client-supplied type.
- **Audit:** deletion and purge are recorded in the audit log without document content.

### 6.2 Answering
- Hybrid retrieval: keyword search + vector search, results merged; optional reranking.
- Answer generated **only** from retrieved passages, returned as structured output:
  answer text, citations (document, section/page, quoted span), confidence, and a refusal flag.
- If evidence is weak or missing → polite refusal + suggestion to contact HR (no guessing).
- Conversation context within a session (follow-up questions work).

### 6.3 Feedback & review
- Employees can mark answers helpful / not helpful with an optional comment.
- Admins see a review queue of low-confidence, refused and down-voted answers.

### 6.4 Analytics
- Questions per day, top topics, refusal rate, helpfulness rate, median response time, cost per answer.

### 6.5 Evaluation (built-in, not optional)
- A versioned evaluation set per tenant: question, expected answer, expected source(s), and
  "unanswerable" questions.
- Eval runs on demand (admin panel) and automatically in CI on every change to retrieval/prompt code.
- Results stored and charted over time.

## 7. Non-functional requirements

### 7.1 Security
- Authentication for all users; role-based access control (RBAC).
- **Tenant isolation enforced in the database** (PostgreSQL Row-Level Security), proven by automated
  cross-tenant tests in CI.
- Secrets only in environment variables; `.env` never committed; secret scanning in CI.
- Input validation on every endpoint; file-type and size limits on uploads.
- Rate limiting per user and per tenant.
- Prompt-injection defenses (content in documents is treated as data, not instructions) + test cases.
- Personal data minimized in logs; log retention limit.
- Audit log for admin actions (upload, delete, role change).

### 7.2 Quality targets (evaluation)
| Metric | Target |
|---|---|
| Retrieval recall@5 | ≥ 0.85 |
| Answer groundedness (claims supported by citations) | ≥ 0.90 |
| Citation accuracy | ≥ 0.90 |
| Correct refusal on unanswerable questions | ≥ 0.90 |

### 7.3 Performance & cost targets
| Metric | Target |
|---|---|
| p95 time to first token (web) | ≤ 3 s |
| p95 full answer time | ≤ 8 s |
| LLM cost per answer | tracked; target ≤ $0.002 on free/cheap models |

### 7.4 Reliability & operations
- Health-check endpoints; structured logs; LLM tracing; error tracking.
- Retries with backoff for LLM and ingestion failures; failed jobs visible, not silent.
- LLM provider behind one interface so providers can be swapped.
- Runbook for deploy, rollback, and common incidents.

### 7.5 Cost constraint
- Build and demo on free tiers. Any paid service must be justified in an ADR with price and alternative.

## 8. Out of scope for v1
- WhatsApp, voice, email channel.
- Actions/tools beyond answering (that is the next project: Support Resolution Agent).
- SSO (Google/Microsoft login) — planned for v2.
- Billing/subscriptions.
- Urdu answers (v2 — after English evals pass).

## 9. Success criteria (portfolio + sales)
- Live demo URL with the demo tenant and a public "try it" account.
- Evaluation report meeting the targets in 7.2, published in `docs/EVAL.md`.
- Security write-up (`docs/SECURITY.md`) with passing tenant-isolation tests.
- 60-second demo video and a written case study.
- Stretch: one real pilot user (a local company) with measured questions answered and time saved.

## 10. Open decisions (to be made one by one, each recorded as an ADR)
1. Monorepo layout (backend + frontend in one repo) — proposed: yes.
2. Auth approach (self-built JWT vs Supabase Auth vs Auth.js).
3. Vector store (pgvector in Postgres — proposed — vs separate vector DB).
4. Embedding model (hosted free tier vs local open-source model).
5. LLM provider(s) for answering and for evaluation judging.
6. Chunking strategy (size, overlap, section-aware splitting).
7. Reranker (none / local cross-encoder / hosted).
8. Hosting targets (API, worker, database, frontend).
9. Observability stack (tracing, error tracking, uptime).

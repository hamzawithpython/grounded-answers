# ADR 0033: `POST /ask` (access, response, limits, metering, provider policy, time budget, logging)

**Status:** Accepted (decisions approved 2026-10-26, with the three changes in "Changes at approval"). Builds on ADR 0030 (retrieval), ADR 0031 (answer model and provider interface) and ADR 0032 (answer contract). Built in three PRs: 5a (this ADR, the metering and settings tables, the admin CLI commands), 5b (query-embedding wiring), 5c (the endpoint).

**Date:** 2026-10-26

**Deciders:** Hamza Asif

## Context

Steps 1 to 4 built retrieval, the provider interface and the checked answer. `POST /ask` joins them behind authentication, and it is the first endpoint that spends money on every call and sends tenant text to a third party. It therefore needs limits, a cost cap that survives restarts, a rule for which providers a tenant may use, and a rule about what we remember. The SPEC targets are a full answer in at most 8 s at p95 and at most $0.002 per answer (7.3).

## Decisions

### 1. Access and request

Any authenticated user of a tenant (`employee` or `tenant_admin`) may ask. The tenant comes from the verified token and the database (`AuthenticatedRequest`), never from the body. The body is `{"question": "..."}` and nothing else (unknown fields are refused with 422, which echoes no input). The question is 1 to 1,000 characters after trimming. Reason for 1,000 and not the retrieval maximum of 2,000: the embedding model reads 512 tokens, so more text is never embedded, and every extra token costs money.

### 2. Response

- **200 `answered`:** `request_id`, `answer`, `citations` of `ref`, `document_id`, `title`, `version`, `clause` (the clause label, or the section when none was found), `heading`, `quote`.
- **200 `refused`:** `request_id`, the reason code and our fixed message (ADR 0032). A refusal is a normal outcome.
- **429:** a rate limit or the daily cap, with `Retry-After` and a fixed message.
- **503:** a provider or service error, Valkey or the database unavailable, the model not loaded, or a timeout. The body is generic. The detail goes to the log as a code.
- `request_id` is a random UUID that lets an operator find the log line. It carries no content.

### 3. Limits and the daily cost cap

- **Rate limits** (Valkey, fail closed like login and upload): one-minute fixed windows, 5 per user and 20 per tenant (settings), counted per attempt before any embedding work. Keys hold an HMAC of the ids. Valkey unavailable gives 503.
- **In-flight limit per tenant:** a setting, default 2 (a Valkey counter with a TTL equal to the deadline). Fail closed. It bounds the overshoot of the cap to the cost of the requests already running.
- **Daily cost cap per tenant:** `tenant_settings.daily_cost_cap_micro_usd`, in whole millionths of a dollar (integers, never floats). Cost is the provider's list price for the tokens used (`answering/pricing.py`), so free tiers are capped as if they were paid (ADR 0031). The day is UTC. The check sums today's `answer_usage` rows for the tenant in the same transaction that retrieves the chunks, and fails closed if the database cannot be read. The demo tenant starts at $0.05 a day.
- **Metering table `answer_usage`** (migration 0017): `tenant_id`, `created_at`, `provider`, `model`, `outcome` (a fixed list), `attempts`, `retrieved_count`, `input_tokens`, `output_tokens`, `cost_micro_usd`, `latency_ms`, `invalid_codes` (a fixed list). **No question, source, answer or quote, and no user id.** The guarantee is in the schema: the only text columns are `provider`, `model`, `outcome` and `invalid_codes`, and CHECK constraints allow only the listed values, so even a bug cannot store a sentence. Tests prove that a sentence is refused in each text column and that the lists match the code. RLS is enabled and forced. `app_user` has `SELECT` and `INSERT` on its own tenant's rows and nothing else: no `UPDATE` or `DELETE` for any role but the owner. Requests refused by a rate limit or by the cap are not metered (they cost nothing and would let an attacker grow the table).
- **Known gaps:** an unparsable provider reply has no token count, so it is stored as 0. A request that dies between the model call and the metering insert leaves no row. The overshoot of the cap is bounded by the in-flight limit.
- **Growth:** rows are never removed in v1. A retention or rollup job is v1.1 work (PROGRESS).

### 4. What we store about questions: counts and codes only

| Option | Pros | Cons |
|---|---|---|
| **A. Counts and codes only (chosen)** | Satisfies SPEC 7.1 (minimise personal data). No retention setting, no purge job. A leak of the metering table reveals no content and no person. | A bad answer cannot be replayed. An admin cannot audit who asked what. Feedback and the review queue (v1.1) need text and will add their own table with a retention rule. |
| B. History with retention | Debuggable. Prepares for v1.1. | Employee questions (HR topics) become sensitive data: a retention job, tests, a SECURITY entry and a deletion path. |

Per-user fairness lives only in expiring Valkey keys. Adding stored text later is a new decision, not a switch.

### 5. Provider per tenant

A required row in `tenant_settings`: `answer_providers` (an ordered list of `groq`, `gemini`, `anthropic`; the first is the primary and a second is the fallback Session 9 may add), `data_class` (`fictional` or `real`, default `real`) and the cap. A tenant with no row, or with no providers, gets no answers and a 503 (fail closed). The row is written only by the operator (admin CLI, `migration_role`); the web app can only read it.

The policy lives in `answering/policy.py`. A provider is usable for a tenant when it is on the tenant's list and, if the tenant's data is `real`, it is in `APPROVED_FOR_REAL_DATA`. Today that set is `{anthropic}` (the client's own paid, no-training key, ADR 0031). Groq and the Gemini free tier are demo-only until a person records otherwise in an ADR. The same function runs in two places: when the operator sets the providers (the CLI refuses a bad combination, and checks the result, so moving a tenant to `real` while Groq is listed is refused), and, in Step 5c, before every call (a refusal is a 503, a `provider_disallowed` row and a log code, never a call). Every settings change by the CLI writes an audit row with the old and new provider list, data class and cap (no secrets, no text).

### 6. Time budget

`/ask` is one JSON response in v1, so "time to first visible state" (SPEC 7.3) is the client's progress state at submit. Server side: an overall deadline of 12 s (a setting; SPEC's 8 s is a p95 target, not a timeout). Per stage: waiting for an embedding slot 2 s, embedding 3 s, retrieval 3 s (a transaction-local `statement_timeout`), each model attempt at most the smaller of 8 s and the time left. No corrective retry when under 4 s remain. When the budget runs out the answer is a 503 "took too long, try again" with `Retry-After`, and a `timeout` row. No database connection is held during the model call: transaction 1 reads the settings and the cap and retrieves; the model call holds nothing; transaction 2 writes the metering row. A provider rate limit (Groq's 8,000 tokens a minute is shared by every tenant) is a 503 with the provider's `Retry-After`, capped, and a `provider_rate_limited` row. The per-tenant cap does not protect that shared limit; Session 9's global daily cap does.

### 7. Query embedding in the web process

Loaded in the background at startup, so `/health` answers at once and `/ready` returns 503 `embedder` until the model is loaded (ADR 0030 said so; the other option, a blocking load, would show as a refused connection instead). A failed load leaves the process unready and logs the code. A switch `ASK_ENABLED` (default off) keeps CI and development light: off means 404 and no model load; on requires `EMBEDDING_MODEL_DIR`. Embedding runs in `asyncio.to_thread` behind a semaphore (default 2, range 1 to 4, set from the measurement in Step 5b). The web process then holds about 0.76 GB steady and 1.1 GB at its peak while loading, as the worker does (ADR 0030, ADR 0022). Tests use a fake embedder; one local `db` test uses the real model.

### 8. Logging

Logged: `request_id`, a keyed hash of the tenant id, provider, model, outcome code, attempts, invalid-output codes, token counts, stage timings, `retrieved_count`. Never logged: the question, a source, the answer, a quote, a user id or an email.

**Project-wide rule (added at approval):** raw tenant, user and document ids stay out of logs on every request and job path (ADR 0017 already kept them out of job logs). Where a log line must correlate requests of one tenant, it carries `HMAC-SHA256(AUTH_LOG_HMAC_KEY, "log:tenant:" + tenant_id)`, hex, first 16 characters. The domain prefix makes the value different from the other uses of the key, and rotating the key breaks correlation but reveals nothing. A grep of `src/` on 2026-10-26 found no log line that carries a raw tenant, user or document id; Step 5c adds the helper and a test that scans `src/` for log calls naming those ids. The sentinel test of Step 5c puts known ids and strings into the question, the chunks, the model's reply and the tenant and user, and asserts that none appears in any captured log record, any metering row, any 503 body or any validation-error body.

## Changes at approval

1. The in-flight limit per tenant is a setting (default 2), not a constant.
2. Logs carry a keyed hash of the tenant id, never the raw id; this is recorded as a project-wide rule (decision 8).
3. Every tenant-settings change through the CLI writes an audit row with the old and new values; tests cover it.

## What Step 5a built

Migration `0017_answer_usage_settings` (`tenant_settings`, `answer_usage`, RLS, the three grants, a backfill of the fail-closed defaults for existing tenants); `answering/policy.py`; `answering/usage.py` (the outcome and invalid-code lists); the ORM models; the admin commands `show-tenant-settings` and `set-tenant-settings`; `create-tenant` now creates the default row; catalog guard and CHECK lists updated; tests for isolation, the grants, the CHECKs (a sentence is refused in every text column), the lists against the code, the CLI and its audit rows, and the backfill.

## Consequences

- A new tenant cannot ask until the operator sets a provider and a cap. That is deliberate.
- Changing the provider list in the CHECK (adding a provider) is a migration and a reviewed change, as is adding an outcome or an invalid-output code.
- The dev database needs `alembic upgrade head` after this PR merges, or `/ready` returns 503 `schema`.
- Not decided here: the global daily cap and the fallback provider (Session 9), the per-tenant bring-your-own-key storage (a future ADR), retention of metering rows (v1.1).

# Architecture — Grounded Answers

**Status:** Draft for review (Session 2, Step 3). Decisions are in `docs/adr/`. This file describes the resulting design.

**Last updated:** 2026-10-22

## 1. Tenancy model

- Every tenant owns its data. Tenant isolation is **enforced in PostgreSQL** with Row-Level Security (RLS). The application can't bypass it by accident.
- A **tenant** is a company (`tenants`). A **user** is a person (`users`), global across tenants. A **membership** links a user to a tenant with a role (`tenant_memberships`). v1 allows one membership per user (ADR 0004).
- Every tenant-owned table has `tenant_id NOT NULL` and an index. Each request sets the tenant context in the database for its transaction (ADR 0005).
- Decisions: [ADR 0004](adr/0004-authentication-self-built-jwt.md) (auth, roles, RLS exceptions), [ADR 0005](adr/0005-database-access-async-sqlalchemy.md) (database access), [ADR 0006](adr/0006-db-test-strategy.md) (tests).

## 2. Data model

```mermaid
erDiagram
    TENANTS ||--o{ TENANT_MEMBERSHIPS : "has"
    USERS ||--o| TENANT_MEMBERSHIPS : "belongs to (v1: one)"
    TENANTS ||--o{ DOCUMENTS : "owns"
    DOCUMENTS ||--o{ DOCUMENT_VERSIONS : "has versions"
    DOCUMENTS |o--o| DOCUMENT_VERSIONS : "current version"
    TENANT_MEMBERSHIPS ||--o{ DOCUMENT_VERSIONS : "uploads"
    TENANT_MEMBERSHIPS ||--o{ AUDIT_LOG : "acts in"
    TENANT_MEMBERSHIPS ||--o{ REFRESH_TOKENS : "holds"
    REFRESH_TOKENS |o--o| REFRESH_TOKENS : "replaced by"
    TENANT_MEMBERSHIPS ||--o{ PASSWORD_RESET_TOKENS : "is target of"
    TENANT_MEMBERSHIPS ||--o{ PASSWORD_RESET_TOKENS : "issued by"

    TENANTS {
        uuid id PK
        text name
        text slug UK
        text status
        timestamptz created_at
    }
    USERS {
        uuid id PK
        text email UK
        text display_name
        text password_hash
        bool active
        timestamptz created_at
        timestamptz updated_at
    }
    TENANT_MEMBERSHIPS {
        uuid id PK
        uuid tenant_id FK
        uuid user_id FK
        text role
        bool active
        timestamptz created_at
        timestamptz updated_at
    }
    DOCUMENTS {
        uuid id PK
        uuid tenant_id FK
        uuid current_version_id FK
        text title
        timestamptz deleted_at
        timestamptz purge_after
        timestamptz created_at
        timestamptz updated_at
    }
    DOCUMENT_VERSIONS {
        uuid id PK
        uuid tenant_id FK
        uuid document_id FK
        int version_no
        text original_filename
        text content_type
        bigint size_bytes
        bytea sha256
        text storage_key UK
        text status
        text failure_reason
        uuid created_by FK
        timestamptz retired_at
        timestamptz purge_after
        timestamptz created_at
    }
    AUDIT_LOG {
        uuid id PK
        uuid tenant_id FK
        uuid actor_user_id FK
        text action
        text target_type
        uuid target_id
        bytea ip_hmac
        jsonb detail
        timestamptz created_at
    }
    REFRESH_TOKENS {
        uuid id PK
        uuid tenant_id FK
        uuid user_id FK
        uuid replaced_by FK
        uuid family_id
        bytea token_hash UK
        timestamptz issued_at
        timestamptz expires_at
        timestamptz used_at
        timestamptz revoked_at
        timestamptz created_at
    }
    PASSWORD_RESET_TOKENS {
        uuid id PK
        uuid tenant_id FK
        uuid user_id FK
        uuid created_by FK
        bytea token_hash UK
        timestamptz expires_at
        timestamptz used_at
        timestamptz created_at
    }
```

The diagram shows key columns only. Section 3 has the full definitions.

## 3. Tables

### Keys used on tenant-owned tables

Every tenant-owned table has:
- `PRIMARY KEY (id)`. Surrogate UUID, generated in the app (ADR 0005).
- `UNIQUE (tenant_id, id)`. Composite foreign keys reference this constraint, so a row can only point at rows in its own tenant.
- `tenant_id uuid NOT NULL`, indexed.
- `created_at timestamptz NOT NULL DEFAULT now()`.

`tenants` and `users` are not tenant-owned. `tenants` *is* the tenant, and `users` is global identity (ADR 0004).

### `updated_at` trigger

`updated_at` on `users`, `tenant_memberships` and `documents` is maintained by one shared trigger function, `grounded.set_updated_at()`:

```sql
CREATE FUNCTION grounded.set_updated_at() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, grounded
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;
-- one BEFORE UPDATE trigger per table, e.g.:
-- CREATE TRIGGER documents_set_updated_at BEFORE UPDATE ON grounded.documents
--   FOR EACH ROW EXECUTE FUNCTION grounded.set_updated_at();
```

- Not `SECURITY DEFINER`: it runs with the caller's privileges and writes only the row being updated, so it needs no elevation.
- `search_path` is pinned so an object in another schema can't shadow `now()`.
- The app never sets `updated_at` itself. The trigger is the single source of truth.

### `tenants`

| Column | Type | Constraints |
|---|---|---|
| id | uuid | PRIMARY KEY |
| name | text | NOT NULL, CHECK length 1–200 |
| slug | text | NOT NULL, UNIQUE, CHECK `slug ~ '^[a-z0-9-]{3,40}$'` |
| status | text | NOT NULL, CHECK in (`active`, `suspended`), DEFAULT `active` |
| created_at | timestamptz | NOT NULL DEFAULT now() |

Suspension blocks access without deleting data (ADR 0004, "Suspended tenants").

### `users` (global)

| Column | Type | Constraints |
|---|---|---|
| id | uuid | PRIMARY KEY |
| email | text | NOT NULL, UNIQUE, CHECK `email = lower(email)` |
| display_name | text | NOT NULL, CHECK length 1–120 |
| password_hash | text | NULL until setup. NULL means "reset not completed". Read only through the login function. |
| active | bool | NOT NULL DEFAULT true |
| created_at | timestamptz | NOT NULL DEFAULT now() |
| updated_at | timestamptz | NOT NULL DEFAULT now() |

### `tenant_memberships`

| Column | Type | Constraints |
|---|---|---|
| id | uuid | PRIMARY KEY |
| tenant_id | uuid | NOT NULL, FK → `tenants(id)`, indexed |
| user_id | uuid | NOT NULL, FK → `users(id)` |
| role | text | NOT NULL, CHECK in (`tenant_admin`, `employee`) |
| active | bool | NOT NULL DEFAULT true |
| created_at | timestamptz | NOT NULL DEFAULT now() |
| updated_at | timestamptz | NOT NULL DEFAULT now() |

Constraints: `UNIQUE (tenant_id, id)`, `UNIQUE (user_id)` (one membership per user in v1), `UNIQUE (tenant_id, user_id)` (target of composite foreign keys).

`platform_owner` is not a role here. Platform work runs through the admin CLI with the migration role (ADR 0004).

### `documents`

| Column | Type | Constraints |
|---|---|---|
| id | uuid | PRIMARY KEY |
| tenant_id | uuid | NOT NULL, indexed |
| title | text | NOT NULL, CHECK length 1–200 |
| current_version_id | uuid | NULL. Composite FK `(tenant_id, current_version_id)` → `document_versions(tenant_id, id)`, `DEFERRABLE INITIALLY DEFERRED`. NULL skips the check. |
| deleted_at | timestamptz | NULL. Set when an admin deletes the document. |
| purge_after | timestamptz | NULL. Set together with `deleted_at` (`deleted_at + DOCUMENT_RETENTION_DAYS`). |
| created_at | timestamptz | NOT NULL DEFAULT now() |
| updated_at | timestamptz | NOT NULL DEFAULT now() |

Constraints: `UNIQUE (tenant_id, id)`. Metadata only; file bytes are in storage.

- `CHECK ((deleted_at IS NULL) = (purge_after IS NULL))`. Both are set together or neither is set.

### `document_versions`

| Column | Type | Constraints |
|---|---|---|
| id | uuid | PRIMARY KEY |
| tenant_id | uuid | NOT NULL, indexed |
| document_id | uuid | NOT NULL. Composite FK `(tenant_id, document_id)` → `documents(tenant_id, id)`. |
| version_no | int | NOT NULL, CHECK ≥ 1, UNIQUE `(document_id, version_no)` |
| original_filename | text | NOT NULL, CHECK length 1–255. Sanitized on input (path separators, control characters and NUL removed). Display only; never used for paths or type decisions. |
| content_type | text | NOT NULL, CHECK in (`application/pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `text/plain`, `text/markdown`). Set by the server's signature detector, not the client's header. |
| size_bytes | bigint | NOT NULL, CHECK `size_bytes BETWEEN 1 AND 10485760` (10 MiB) |
| sha256 | bytea | NOT NULL, CHECK `octet_length(sha256) = 32`. Dedupe is per tenant only (see 5.4). |
| storage_key | text | NOT NULL, UNIQUE, CHECK `storage_key LIKE 'tenant/' \|\| tenant_id::text \|\| '/%'`. Server-generated (`tenant/<tenant_id>/<uuid4>`), never derived from the filename. |
| status | text | NOT NULL, CHECK in (`queued`, `processing`, `ready`, `failed`, `retired`, `purging`) |
| failure_reason | text | NULL, CHECK length ≤ 500. Shown to admins. Never contains file content. |
| created_by | uuid | NOT NULL. Composite FK `(tenant_id, created_by)` → `tenant_memberships(tenant_id, user_id)`. |
| retired_at | timestamptz | NULL. Set when replaced or deleted. |
| purge_after | timestamptz | NULL. Set together with `retired_at`. |
| created_at | timestamptz | NOT NULL DEFAULT now() |

Constraints: `UNIQUE (tenant_id, id)`.

- `CHECK ((retired_at IS NULL) = (purge_after IS NULL))`. Both are set together or neither is set.
- `CHECK ((status IN ('retired', 'purging')) = (retired_at IS NOT NULL))`. A version is retired exactly when it's `retired` or `purging`.

**Status meanings:** `queued` and `processing` are ingestion. `ready` can be cited. `failed` is an ingestion failure with a reason. `retired` is replaced or deleted and waiting for purge. `purging` means the purge job has started and the row will be removed.

**Content detection** (server-side, the client header is ignored):
- **PDF:** starts with `%PDF-`.
- **DOCX:** a ZIP archive (`PK\x03\x04`) that contains `[Content_Types].xml` and `word/document.xml`. The ZIP check alone would accept any ZIP.
- **TXT and Markdown:** no magic bytes. The detector checks for valid UTF-8 with no NUL bytes. The `text/plain` vs `text/markdown` label comes from the extension allow-list, so it's a label, not a verified fact.

### `document_blobs` (ADR 0018, migration 0007)

The bytes of one document version. Kept apart so that normal document queries never read a file.

| Column | Type | Constraints |
|---|---|---|
| tenant_id | uuid | PK (with version_id), FK `tenants` RESTRICT |
| version_id | uuid | PK; composite FK `(tenant_id, version_id)` to `document_versions` RESTRICT |
| content | bytea | NOT NULL (deferred in the ORM) |
| byte_size | integer | CHECK 1 to 10485760; CHECK `octet_length(content) = byte_size` |
| created_at | timestamptz | NOT NULL, default `now()` |

Policies (ADR 0018, amendment): `app_user` INSERT only, for a `queued` version of its tenant. `worker_user` SELECT for `queued`, `processing` and `purging` versions. `worker_user` DELETE for `purging` versions. RLS is enabled and forced.

### `audit_log` (append-only)

| Column | Type | Constraints |
|---|---|---|
| id | uuid | PRIMARY KEY |
| tenant_id | uuid | NOT NULL, indexed with `created_at` |
| actor_user_id | uuid | NULL (NULL for system actions such as purge). Composite FK `(tenant_id, actor_user_id)` → `tenant_memberships(tenant_id, user_id)`. |
| action | text | NOT NULL, CHECK length 1–64. Examples: `login.success`, `login.failure`, `user.create`, `document.delete`, `document.purged`. |
| target_type | text | NULL |
| target_id | uuid | NULL |
| ip_hmac | bytea | NULL. HMAC-keyed IP (ADR 0004). Never a raw IP. |
| detail | jsonb | NULL. Small structured context. No secrets, no personal data, no file content, filenames or titles. |
| created_at | timestamptz | NOT NULL DEFAULT now() |

Append-only by grants: `app_user` has `INSERT` and `SELECT` only.

### `tenant_settings` (ADR 0033, migration 0017)

One row per tenant, written only by the operator (admin CLI, `migration_role`). The web app reads it. A tenant with no row, or with no providers, gets no answers (fail closed).

| Column | Type | Notes |
|---|---|---|
| `tenant_id` | uuid PK | FK to `tenants` |
| `answer_providers` | text[] | Ordered, from `groq`, `gemini`, `anthropic` (CHECK), at most 3. Default empty |
| `data_class` | text | `fictional` or `real` (CHECK). Default `real` |
| `daily_cost_cap_micro_usd` | bigint | 0 to 1,000,000,000 (CHECK). Default 0 |
| `created_at` | timestamptz | |

`answering/policy.py` decides which providers a data class allows; the admin CLI applies it when settings change and each change is audited (`tenant_settings.update`, old and new values).

### `answer_usage` (ADR 0033, migration 0017)

One row per `/ask` request that reached the answer stage. **Codes and numbers only.** The only text columns are `provider`, `model`, `outcome` and `invalid_codes`, and CHECK constraints limit all but `model` (length 1 to 200) to fixed lists, so the question, a source, an answer or a quote cannot be stored. No user id.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid | FK to `tenants`; index `(tenant_id, created_at)` serves the daily-cap sum |
| `created_at` | timestamptz | |
| `provider`, `model` | text, nullable | Null when the request failed before a provider was chosen |
| `outcome` | text | One of `answering/usage.py` `OUTCOMES` |
| `attempts` | smallint | 0 to 2 |
| `retrieved_count` | smallint | 0 to 20 |
| `input_tokens`, `output_tokens` | integer | 0 to 10,000,000 |
| `cost_micro_usd` | bigint | List-price cost in millionths of a dollar |
| `latency_ms` | integer | |
| `invalid_codes` | text[] | At most 2, from `INVALID_CODES` |

`app_user` may `SELECT` and `INSERT` its own tenant's rows. Nobody but the owner can `UPDATE` or `DELETE`. Rows are never removed in v1 (retention or rollup is v1.1 work).

### `refresh_tokens`

| Column | Type | Constraints |
|---|---|---|
| id | uuid | PRIMARY KEY |
| tenant_id | uuid | NOT NULL, indexed |
| user_id | uuid | NOT NULL. Composite FK `(tenant_id, user_id)` → `tenant_memberships(tenant_id, user_id)`. Indexed. |
| family_id | uuid | NOT NULL, indexed. Revoking a family revokes every rotation in it. |
| replaced_by | uuid | NULL. Composite FK `(tenant_id, replaced_by)` → `refresh_tokens(tenant_id, id)`. |
| token_hash | bytea | NOT NULL, UNIQUE, CHECK `octet_length(token_hash) = 32`. SHA-256 of the token. Never the token. |
| issued_at | timestamptz | NOT NULL |
| expires_at | timestamptz | NOT NULL (14 days after `issued_at`) |
| used_at | timestamptz | NULL. Set on rotation. Reuse detection depends on it. |
| revoked_at | timestamptz | NULL |
| created_at | timestamptz | NOT NULL DEFAULT now() |

Constraints: `UNIQUE (tenant_id, id)`.

### `password_reset_tokens`

| Column | Type | Constraints |
|---|---|---|
| id | uuid | PRIMARY KEY |
| tenant_id | uuid | NOT NULL, indexed |
| user_id | uuid | NOT NULL. Composite FK `(tenant_id, user_id)` → `tenant_memberships(tenant_id, user_id)`. Indexed. |
| created_by | uuid | NOT NULL. Composite FK `(tenant_id, created_by)` → `tenant_memberships(tenant_id, user_id)`. |
| token_hash | bytea | NOT NULL, UNIQUE, CHECK `octet_length(token_hash) = 32` |
| expires_at | timestamptz | NOT NULL, CHECK `expires_at > created_at` (24 hours) |
| used_at | timestamptz | NULL. One use only. |
| created_at | timestamptz | NOT NULL DEFAULT now() |

Constraints: `UNIQUE (tenant_id, id)`.

## 4. Foreign keys and `ON DELETE` rules

**Every foreign key is `ON DELETE RESTRICT` and `ON UPDATE RESTRICT`.** Nothing cascades. Ids never change, so `ON UPDATE` is restricted too.

- A parent row with children can't be deleted. The app must delete children first, in the order in section 5.
- A composite foreign key includes `tenant_id`, so a child can only reference a parent in the same tenant. The database rejects a cross-tenant reference even if the app has a bug.
- A tenant is **suspended, not deleted**, in v1. There is no tenant-delete path. If one is added later, it gets its own ADR and purge job.

| Child → Parent | Columns | Notes |
|---|---|---|
| tenant_memberships → tenants | `tenant_id` | |
| tenant_memberships → users | `user_id` | Global identity. |
| documents → document_versions | `(tenant_id, current_version_id)` | Deferred. NULL skips the check. |
| document_versions → documents | `(tenant_id, document_id)` | |
| document_versions → tenant_memberships | `(tenant_id, created_by)` | |
| audit_log → tenant_memberships | `(tenant_id, actor_user_id)` | NULL skips the check (system actions). |
| refresh_tokens → tenant_memberships | `(tenant_id, user_id)` | |
| refresh_tokens → refresh_tokens | `(tenant_id, replaced_by)` | Rotation chain. |
| password_reset_tokens → tenant_memberships | `(tenant_id, user_id)` | |
| password_reset_tokens → tenant_memberships | `(tenant_id, created_by)` | |

## 5. Document lifecycle

### 5.1 States

- **Document:** `active` (`deleted_at IS NULL`) → `deleted` (`deleted_at` set, `purge_after` set) → purged (row removed).
- **Version:** `queued` → `processing` → `ready` → `retired` → `purging` → removed. `failed` can be retired too.

### 5.2 Upload and replace

1. Upload creates a `document_versions` row with `status = queued`. The bytes are stored under the server-generated `storage_key`.
2. Ingestion moves the version through `processing` to `ready` (or `failed` with a reason).
3. **Swap, in one transaction (ADR 0024):** the worker's `ready` write and a call to the definer function `swap_current_version` happen together. A version becomes current only if its `version_no` is higher than the current one's, or the document has no current version. Then the old current version becomes `retired`, with `retired_at = now()` and `purge_after = now() + DOCUMENT_RETENTION_DAYS`. A finished version that is older than the current one is retired at once, as superseded.

Answers cite only the current `ready` version. A `queued` or `processing` version is never cited.

### 5.3 Delete

One transaction (ADR 0025): set `documents.deleted_at = now()` and `purge_after = now() + DOCUMENT_RETENTION_DAYS`. Set every version that is not already `retired` or `purging` to `retired` with `retired_at` and `purge_after`. Write audit entry `document.delete` (counts only, no title, no filename). After commit, retrieval excludes the document (section 5.6). A deleted or unknown document returns 404, with the same body. A repeat delete changes nothing. Migration 0012 refuses any attempt by a guarded role to clear `deleted_at`, so a delete cannot be undone through the database (ADR 0025, amendment). Retention is `TEMPORARY_RETENTION_DAYS` (30) in `documents_api.py` until PR 7 adds `DOCUMENT_RETENTION_DAYS`.

**Restore is not supported in v1.**

### 5.4 Deduplication

`sha256` dedupe is **per tenant only**. Cross-tenant dedupe would reveal that another tenant holds the same file. Duplicate detection queries `(tenant_id, sha256)`, which is indexed but not unique.

### 5.5 Purge (retention job, runs in the Session 3 worker)

The purge job runs as **`worker_user`**, a dedicated LOGIN role with `NOBYPASSRLS` that is not a table owner (ADR 0007).

**Step 0: find due tenants.** The worker calls `grounded.retention_due_tenant_ids()`, a narrow `SECURITY DEFINER` function (owner `retention_definer`, hardened like the auth functions). It returns **only tenant ids** that have a due purge (`purge_after <= now()`), with no document data. The worker then processes each tenant separately:

```text
for tid in retention_due_tenant_ids():
    with tenant transaction(tid):          # set_config('app.current_tenant', tid, true)
        for each due version and deleted document in this tenant:
            run steps 1-6 below
```

Each tenant is processed under normal RLS, so the worker can't read or change another tenant's rows, even if the code has a bug.

**Built design (ADR 0027):** the sweep reads the due items for each tenant inside that tenant's transaction, and defers **one job per item** with a queueing lock (`purge:<kind>:<id>`), so repeated sweeps do not pile up jobs. Each job runs the steps below in its own tenant transaction. A crash rolls the item back, and the next sweep defers it again.

For each due item, in this order (children first, because of RESTRICT):

1. **Mark** versions `purging` in a transaction. Retrieval already excludes them.
2. **Delete dependent data** (chunks and embeddings, from Session 3/4; see section 6).
3. **Delete the bytes row** in `document_blobs` for the version (ADR 0018). Idempotent: a missing row counts as deleted. This must run before the version row is deleted, because the foreign key is RESTRICT.
4. **Delete the version row.** For a deleted document, first set `documents.current_version_id = NULL`.
5. **Delete the document row** once it has no versions left.
6. **Write audit entry** `document.purged` or `document_version.purged`, with `actor_user_id = NULL`, `tenant_id` set, and only counts in `detail`, for example `{"versions": 2, "chunks": 147, "bytes_deleted": true}`. No title, filename, hash or content.

**Crash safety:** a crash can leave versions in `purging`. The job is idempotent and resumes them. Retrieval excludes `purging` rows, so a half-purged document is never cited.

**Retention is recorded per row.** `purge_after` is written when the row is retired or deleted. Changing `DOCUMENT_RETENTION_DAYS` affects only rows retired after the change.

### 5.6 Retrieval rule (required)

An answer may cite only rows where **all** of these hold:
- `documents.deleted_at IS NULL`,
- the document's `current_version_id` points at this version,
- `document_versions.status = 'ready'`.

Chunks inherit this rule once they exist (section 6).

### 5.7 Required tests

1. **Immediate exclusion:** after a delete commits, the retrieval query returns no rows for that document. Same for a replaced version after the swap commits. Extended to chunks in Session 3/4.
2. **Purge completeness:** after purge, no `document_versions`, `documents`, stored bytes or dependent rows remain for that document. Session 3/4 adds dependent rows.
3. **Purge is idempotent:** a purge interrupted after the byte deletion but before the row deletion completes correctly when re-run.
4. **Content-free audit:** the `document.purged` entry's `detail` contains only allowed count keys. The test fails if it contains a title, filename, hash or any string from the document.
5. **Queued versions are not cited:** a `queued` or `processing` version never appears in retrieval.
6. **Tenant scoping:** tenant A's purge never touches tenant B's rows or bytes (proven through the composite keys and RLS).

### 5.8 Settings

- `DOCUMENT_RETENTION_DAYS`: integer, default `30`, range `1–365`. Validated in `Settings` (web app) and `WorkerSettings` (worker) at startup. Built in Step 7, PR 7 (ADR 0027). Each retirement records its own `purge_after` from it, so a later change affects only later retirements.

### 5.10 Step 7 jobs and endpoints (ADR 0023 to 0029)

| Item | Kind | Schedule or trigger | Definer function | ADR |
|---|---|---|---|---|
| Stalled-version sweep | The worker's timer, once at start and then every five minutes (worker_user calls the definer) | Every five minutes | `enqueue_stalled_ingest` (`retry_definer`) | 0023 (amended, PR 9) |
| Purge sweep and purge jobs | The worker's timer runs the sweep (purge_definer defers one job per due item); each job is a Procrastinate job (worker_user, one transaction per item) | Every fifteen minutes | `enqueue_purge_jobs` (`purge_definer`) | 0027 (amended, PR 9) |
| Queue cleanup | The worker's timer, once at start and then every 24 hours (worker_user calls the definer) | Every 24 hours | `cleanup_finished_jobs` (`queue_cleanup_definer`) | 0028 (amended, PR 9) |
| `swap_current_version` | Called by the worker in its finish transaction | Each replacement | `swap_definer` | 0024 |

| Endpoint | Role | ADR |
|---|---|---|
| `POST /documents/{id}/versions` | tenant_admin | 0024 |
| `DELETE /documents/{id}` | tenant_admin | 0025 |
| `GET /documents` (cursor pagination) | tenant_admin | 0029 |

The status transitions that each role may make are in ADR 0026.

## 5.9 Text extraction (Session 4, Step 4, ADR 0020)

The worker reads a version's bytes (`ObjectStore`, ADR 0018) and extracts text in a child process:

- The parent starts `python -I -m grounded_answers.extract.child KIND [limit]`, and writes the bytes to its stdin. The child returns one JSON object on stdout. Nothing is written to disk.
- Limits: a timeout per format (PDF 30 s, DOCX 10 s, Markdown and text 5 s), 50 MiB of output, and on Linux a 1 GiB address-space limit set in the child before the parsers load. The parent kills the whole process tree on a limit.
- Libraries: pypdf (PDF), python-docx (DOCX, entities not resolved), markdown-it-py (Markdown and text).
- Output is one structure for every format: `Extracted` with `Section`s (number, heading, level, page, text) and `Table`s. Section numbers set the level; font size sets it for unnumbered PDF headings.
- Failures are one of four reasons: `timeout`, `memory`, `unreadable`, `no_text`. Nothing from the child's stderr or stdout reaches a log or a reason.

## 6. Chunks, embeddings and retrieval (ADR 0021, ADR 0030, migrations 0008 and 0016)

One row per chunk of a document version, key `(tenant_id, version_id, position)`. Columns: `section_number`, `heading`, `clause_labels` (for citations such as 3.2.2), `body`, `embedding vector(384)`, `embedding_model` and `embedding_model_version` (pinned, ADR 0021 decision 6), and `tsv`, a generated `tsvector` over `body` with the English configuration. Indexes: the primary key (tenant first) and GIN on `tsv`. **There is no vector index**: migration 0016 dropped HNSW (ADR 0030).

Composite FK `(tenant_id, version_id)` to `document_versions`, `ON DELETE RESTRICT`. Chunks are purged before their version (section 5.5).

**Row-level security is the retrieval rule (5.6).** `app_user` can SELECT a chunk only when its version is `ready`, is the document's `current_version_id`, and the document is not deleted. So a retrieval query needs no tenant or version filter of its own, and a retired, processing, failed or deleted version is invisible to it. `worker_user` reads, deletes and inserts chunks only for `processing` (and, for read and delete, `purging`) versions.

**Retrieval (`grounded_answers.retrieval`, ADR 0030).** `retrieve(session, question, query_vector, top_k=...)` runs inside the caller's tenant transaction as `app_user` and returns the top `RETRIEVAL_TOP_K` chunks (default 5) with the text, document title, version number, section, heading and clause labels.

```mermaid
flowchart LR
  Q[question] --> E[embed with the query: prefix, in a thread]
  E --> V[vector: exact cosine over the tenant's rows]
  Q --> K[keyword: stemmed words ORed, ts_rank_cd]
  V --> F[reciprocal rank fusion, constant 60]
  K --> F
  F --> M[metadata: title, version, labels]
  M --> R[top_k chunks]
```

- **Vector.** No index, so the planner narrows by the tenant-first primary key and sorts the tenant's rows by exact cosine distance. Correct at every size tested; the cost grows with the tenant's own chunk count (docs/EVAL.md). Revisit thresholds are in the RUNBOOK.
- **Keyword.** English configuration; it cannot match Roman Urdu or Urdu script (ADR 0021). A question with no keyword match keeps the vector order, because fusion adds nothing from an empty list.
- **Ties** are broken by the md5 of the chunk body, then `(version_id, position)`. `version_id` is a random UUID and on its own would make two loads of the same files rank differently.
- **Query embedding in the web process (decided, built with `POST /ask`):** the model loads once in the app lifespan before the app is ready, each question is embedded with `asyncio.to_thread` bounded to about 4 at a time, and `/ready` fails until the model is loaded. Each process holds about 0.76 GB (peak 1.1 GB while loading), so run one worker per container.
- **Known limit.** Table chunks carry only the section label (for example `6`), not the clause of the table's caption (`6.2`), so a citation of a table is section-level until ingestion is changed (PROGRESS, q012).

### 6.1 The answer path (Session 5, Step 4, ADR 0031 and ADR 0032; built, not yet behind `POST /ask`)

`answer_question(model, question, chunks)` turns the retrieved chunks into a checked answer or a refusal. The model is behind the provider-neutral `AnswerModel` interface (ADR 0031). The model sees only the question and the chunk text in `<source id="n">` blocks. Titles, versions, ids and clause labels stay on our side and are attached to the citations afterwards.

```mermaid
flowchart TD
  C[retrieved chunks] --> N{any chunks?}
  N -- no --> R1[refusal: not_in_documents, no model call]
  N -- yes --> A[ask the model: rules, source blocks, question]
  A -- provider failed --> ERR[AnswerServiceError, an error and not a refusal]
  A -- safety block --> R2[refusal: declined]
  A --> P[parse the JSON contract]
  P -- model refused --> R3[refusal with our wording]
  P -- answer --> V[validate: refs, quotes, table rule, numbers]
  P -- not valid JSON or cut off --> BAD[invalid output]
  V -- invalid --> BAD
  BAD -- first time --> A2[ask again with the fixed corrective note]
  A2 --> P
  BAD -- second time --> R4[refusal: could_not_verify]
  V -- valid --> OK[answer with citations: chunk, quote, clause]
```

- **Contract:** `refused`, `refusal_reason`, `answer`, `citations` (each `ref` and `quote`). No confidence field.
- **Validation** (`validate.py`): the ref is one of the k sources; the normalised quote appears in that source; at least 20 characters (8 and a word for a table chunk); no ellipsis; every number of the answer appears in a quote. Any failure invalidates the whole answer. It proves the quote exists, not that the answer is fully supported (Step 7).
- **Clause** (`clauses.py`): the nearest label at or before the quote, within the chunk's own section; the section when there is none; the section for a table.
- **Retry:** one, with a fixed note; the second failure is `could_not_verify`. Broken output is never shown.
- **Logs:** provider, model, outcome, attempts, invalid-output codes and token counts only.

**Step 6 additions (ADR 0034).** The prompt builder removes invisible characters (tag characters, bidirectional controls, U+200B, U+FEFF; the joiners U+200C and U+200D stay) and escapes `<source`, `</source` and `<|` before the model sees a source or the question; `normalise` ignores the same characters, and a quote copied from the escaped text is accepted. Validation gains four rules: a `[n]` marker must name a given source, the answer text must not be mostly non-Latin script (`answer_language`), and it must not repeat 8 words of the rules (`prompt_echo`); quotes are never checked for language. Migration 0018 adds the two codes to the CHECK list of `answer_usage.invalid_codes`.

### 6.2 Query embedding in the web process (Session 5, Step 5b, ADR 0033 decision 7)

With `ASK_ENABLED=true` the web process loads the pinned embedding model after startup, in a background thread, so the app takes requests at once and `/ready` answers 503 `embedder` until the model is loaded (and stays 503 if the load fails). Embedding a question runs in a thread behind a semaphore of `EMBED_CONCURRENCY` slots (default 2). A caller waits at most 2 s for a slot (then 503 busy) and at most 3 s for the embedding (then 503 took too long). The process then holds about 0.76 GB steady and 1.1 GB at the peak of the load, whatever the concurrency (`docs/EVAL.md`). With `ASK_ENABLED` off (the default) none of this exists: no model is loaded and `/ready` does not wait for one.

### 6.3 The `/ask` flow (Session 5, Steps 5c-1 and 5c-2, ADR 0033)

```mermaid
flowchart TD
  A[verified caller + question] --> B{rate limit<br/>user and tenant}
  B -- over --> B1[429 rate_limited]
  B -- Valkey down --> X[503]
  B --> C{metering flag set?}
  C -- yes --> X
  C --> D{in-flight slot free?}
  D -- no --> D1[429 busy]
  D --> E[read settings + today's spend]
  E -- DB down or slow --> X
  E -- no row or no providers --> E1[503 not_configured, metered]
  E -- spend at or over cap --> E2[429 daily_limit, not metered]
  E --> F{provider allowed and key present?}
  F -- no --> F1[503, metered, no call]
  F --> G[embed] --> H[retrieve] --> I[answer_question<br/>validate, one corrective retry]
  G -- busy, timeout, not ready --> X2[503 try_again, metered]
  H -- DB error or timeout --> X2
  I -- provider error or deadline --> X2
  I --> J[metering row, tried twice]
  J -- both fail --> K[set tenant flag, still answer]
  J --> L[answered or refused, 200]
  K --> L
```

The slot taken at "in-flight" is released in every case, including an exception and a cancelled request. No database connection is held while the model is called: settings, retrieval and metering each run in their own short transaction. Every stage has a timeout inside the 12 s deadline; the metering write runs after it. Logs carry a request id, a keyed hash of the tenant id (`log_ref.py`), codes, counts and timings, and nothing else.

### 6.4 `POST /ask` over HTTP (Session 5, Step 5c-2, ADR 0033)

Mounted only when `ASK_ENABLED` is on. `authenticated_principal` verifies the token and re-checks the caller in the database like every protected route, then releases its connection before the route runs; the route takes the tenant and the user from the principal and never from the body. The flow of 6.3 does the work, each stage in its own short transaction (`ask/store.py`). The response is one of: 200 answered (answer and citations with document, version, clause or section, heading and quote), 200 refused (a code and our fixed message), 429 (`rate_limited`, `busy`, `daily_limit`, with `Retry-After`), 503 (`not_configured`, `try_again`, `service_unavailable`) or 500 (no detail). All carry `Cache-Control: no-store` and a `request_id`. The provider registry holds an adapter only for a configured key; a tenant's first listed provider is the only one used (fallback is Session 9).

## 7. Row-Level Security summary

| Table | RLS | Policies |
|---|---|---|
| `tenants` | ENABLE + FORCE | `app_user`: SELECT own row. `auth_definer`: SELECT (login). Migration role: all (operator). |
| `users` | ENABLE + FORCE | `app_user`: SELECT and UPDATE through a membership in the current tenant. **No INSERT**: employees are created by `create_tenant_user` (ADR 0004 amendment). `provisioning_definer`: INSERT, `WITH CHECK (true)` (users is global; the membership insert in the same function is the gate). `auth_definer`: SELECT (login). |
| `tenant_memberships` | ENABLE + FORCE | `app_user`: tenant-scoped SELECT and UPDATE. **No INSERT** (only `create_tenant_user`). `provisioning_definer`: SELECT and INSERT, scoped to the session tenant. `auth_definer`: SELECT (login). |
| `documents` | ENABLE + FORCE | `app_user`: scoped to current tenant. `worker_user`: scoped to current tenant, SELECT and DELETE (DELETE only when deleted and with no versions left). |
| `document_versions` | ENABLE + FORCE | `app_user`: scoped to current tenant. `worker_user`: scoped to current tenant. UPDATE only `retired` to `purging`. DELETE only when `purging`. |
| `audit_log` | ENABLE + FORCE | `app_user`: scoped to current tenant, `INSERT` and `SELECT` only. `worker_user`: `INSERT` only. |
| `tenant_settings` | ENABLE + FORCE | `app_user`: `SELECT`, own tenant. `migration_role`: all (the admin CLI). |
| `answer_usage` | ENABLE + FORCE | `app_user`: `SELECT` and `INSERT`, own tenant (`WITH CHECK`). `migration_role`: all. No `UPDATE` or `DELETE` for any app role. |
| `refresh_tokens` | ENABLE + FORCE | `app_user`: scoped to current tenant. `auth_definer`: SELECT (refresh). |
| `password_reset_tokens` | ENABLE + FORCE | `app_user`: scoped to current tenant (SELECT, INSERT, UPDATE). |
| `tenants` | ENABLE + FORCE | `app_user`: SELECT own row only. `auth_definer`: SELECT (login, `status` only). `migration_role`: all (operator). |
| `tenant_email_domains` | ENABLE + FORCE | `app_user`: SELECT, own tenant. `provisioning_definer`: SELECT, own tenant. `migration_role`: all (operator, the only writer). |
| `grounded.retention_due_tenant_ids()` | n/a (function) | Returns tenant ids only. Owner `retention_definer`, which has SELECT on `documents` and `document_versions` through `USING (true)` policies limited to that role. |
| `queue.*` (Procrastinate job queue, ADR 0015, 0016) | **none, by design** | No tenant column, so no RLS. Carries ids and job state only. Grants are the reviewed list in `tests/db/expected_queue_grants.json`; PUBLIC has none. Residual risk R17. |

- "Scoped to current tenant" means `tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid`. With no tenant set, every policy returns zero rows (fail closed).
- **Tenant-owned tables are never written without a tenant.** `app_user`'s INSERT on `users` is the only INSERT that isn't tenant-scoped at the policy level. It's safe because a user row has no tenant access until a membership row exists, and the membership insert is tenant-scoped.

### 7.1 Database roles

| Role | Login | Attributes | Purpose |
|---|---|---|---|
| `migration_role` | yes (`MIGRATION_DATABASE_URL`) | NOSUPERUSER, NOBYPASSRLS | Owns the schema, tables, functions and triggers. Runs Alembic and the admin CLI. Never used by the web app or the worker. |
| `app_user` | yes (`DATABASE_URL`) | NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, NOINHERIT | Web app. Tenant-scoped by RLS. |
| `worker_user` | yes (worker only) | NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, NOINHERIT | Retention purge job. Tenant-scoped by RLS, plus one tenant-id function. |
| `auth_definer` | **no** (NOLOGIN) | NOBYPASSRLS | Owns the login and refresh functions (ADR 0004). |
| `retention_definer` | **no** (NOLOGIN) | NOBYPASSRLS | Owns `retention_due_tenant_ids()` (ADR 0007). |
| `provisioning_definer` | **no** (NOLOGIN) | NOBYPASSRLS | Owns `create_tenant_user()` (ADR 0004 amendment). |

- No role has `BYPASSRLS` or `SUPERUSER`, except the cluster admin used only for bootstrap (locally the Docker `postgres` user; on a managed host, the provider's owner role). The app and the worker never connect as that role.
- `NOINHERIT` on login roles prevents them from picking up privileges through membership in the definer roles.
- The bootstrap plan (roles and grants) is in ADR 0006 and the Step 4 SQL. Grants are versioned Alembic migrations, not one-off commands.

## 8. Open items

- **Chunks table:** deferred to Session 3/4, pending the embedding model decision.
- **Document storage backend:** decided in ADR 0018. Bytes are stored in PostgreSQL (`document_blobs`) behind an `ObjectStore` interface. Backblaze B2 is the recorded scale-up path. `storage_key` stays the logical key.
- **Retention decision:** recorded in ADR 0007 (Proposed). Includes the purge worker design, the backup limitation and the open question on the 30-day default.
- **Backups:** purge removes live data, not backup copies. The backup retention window depends on the hosting provider and must be recorded in SECURITY.md and RUNBOOK when the host is chosen.
- **SPEC §6.1:** proposed update for retention and deletion is in the ADR 0007 review. It needs approval before it's applied.

## 9. Data access layer (Session 3, Step 2)

ADR 0005 and ADR 0011 are the sources. The code is in `backend/src/grounded_answers/`:
`models.py` (ORM mirror of the tables), `tenant_db.py` (the only way to start a tenant
transaction), `rowcount.py` (write checks, SECURITY R9), `database.py` (engine and session factory).

### 9.1 One tenant transaction

```mermaid
sequenceDiagram
    participant R as Route (scope="function")
    participant H as begin_tenant_transaction
    participant P as Pool (one connection in tests)
    participant DB as PostgreSQL (app_user, RLS)
    R->>H: context (tenant_id, user_id)
    H->>P: new session, BEGIN
    H->>DB: set_config('app.current_tenant', $1, true), set_config('app.current_user', $2, true)
    R->>DB: queries (filtered by RLS)
    alt handler returns
        H->>DB: COMMIT (before the response is sent)
        H->>P: connection back to pool, settings gone
    else exception or failed commit
        H->>DB: ROLLBACK, settings gone
        H-->>R: exception (500)
    end
```

### 9.2 Authentication (Session 3, Step 3a; ADR 0012)

- Access tokens: EdDSA JWT, 15 minutes, `iss` `grounded-answers-api`, `aud` `grounded-answers-web`, `kid` header
  from the key file (`auth_keys.py`, `auth_tokens.py`).
- Login: `auth_service.login`. Anonymous lookup, Argon2 verify in a worker thread on every path, checks, then
  one tenant transaction for the refresh token and the audit row (`auth_api.py` for the routes).
- Protected routes: `authenticated_context` verifies the token, opens the tenant transaction, and re-reads
  membership, user and tenant status from the database. Role comes from the row.

### 9.2b Refresh and logout (Session 3, Step 3b; ADR 0013)

- `auth_refresh.py`: the cookie is hashed and looked up, then the tenant transaction locks the token row with
  `SELECT ... FOR UPDATE`. Decisions are made on the locked row. Writes are committed before any error reaches
  the client.
- Routes (`auth_api.py`): `POST /auth/refresh`, `POST /auth/logout` (both need an allowed Origin),
  `POST /auth/logout-all` (bearer token).

### 9.2c Login protection (Session 3, Step 3c; ADR 0014)

- `rate_limit.py`: `LoginLimiter` keeps the limits in Valkey. `RECORD_FAILURE_LUA` counts and locks in one
  script. Keys are HMAC'd and expire.
- `timing.py`: `TimingFloor`. Every login response waits until 500 ms have passed (R11).
- Order on login: limit check (before the lookup), lookup, Argon2 verify, checks, then on failure record it.

### 9.3 Rules

| Rule | Enforced by |
|---|---|
| Only `begin_tenant_transaction` sets the tenant or user context | `tests/test_tenant_context_source.py` |
| Context values are transaction-local and bound | `tests/db/test_tenant_transaction.py` (statement order and parameters) |
| No tenant context survives a pooled connection's next transaction | `tests/db/test_tenant_transaction.py` (commit, rollback, error; same backend pid) |
| Every relationship is `lazy="raise"` | `tests/test_models.py` |
| `password_hash` and `updated_at` on users are deferred | `tests/test_models.py`, `tests/db/test_orm_models.py` |
| Models and migrations agree on tables, columns, keys and indexes | `tests/db/test_orm_metadata.py` (Alembic comparison) |
| Models and migrations agree on CHECK names and definitions | `tests/db/test_orm_metadata.py` (`expected_checks.json`) |
| A failed commit is a server error, not a success | `tests/db/test_tenant_dependency.py` |
| A write that affects an unexpected number of rows is refused | `tests/db/test_rowcount.py` |

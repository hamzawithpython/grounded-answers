# ADR 0018: Document Bytes in PostgreSQL for v1, Behind an ObjectStore Interface

**Status:** Accepted

**Date:** 2026-10-23

**Deciders:** Hamza Asif

## Context

Session 4, Step 2 needs somewhere to keep uploaded file bytes (PDF, DOCX, TXT, Markdown, up to 10 MiB each, SPEC §6.1). ARCHITECTURE §3 and §5 already define the design: a server-generated `storage_key` of the form `tenant/<tenant_id>/<uuid4>`, with a database CHECK on its prefix, and an idempotent delete in the purge (ADR 0007).

Three things make the choice harder than it looks:

1. **Outbox.** The upload must commit the version row, the bytes and the ingestion job in one transaction (ADR 0015). With bytes in an external object store, a crash between the object write and the commit leaves an orphaned object, or a row that points at a missing object. Each needs its own cleanup.
2. **Least privilege.** R5 (SECURITY.md) records that a compromised worker can delete stored files. External storage needs its own credentials, one per process, and each is a secret to manage.
3. **Operating cost.** The first version has no budget for a second service, a second account or a card on file.

The options were compared in the Step 2 decision note (2026-10-23). Summary:

| Option | Verdict |
|---|---|
| **A. SeaweedFS in dev, Backblaze B2 in production** | Meets least privilege, zero cost at our scale. Needs a second service in dev and in CI, a second account, and a card-verification check at sign-up that is unresolved. Adds the orphan cleanup in point 1. |
| **B. SeaweedFS in dev, Cloudflare R2 in production** | Rejected. R2's token levels cannot separate write from object delete (Cloudflare's token documentation, checked 2026-10-23), so the web app and the worker would share a delete capability. |
| **C. SeaweedFS in dev, AWS S3 in production** | Rejected for v1. The official AWS page says new accounts from 15 July 2025 get up to $200 in credits. Third-party sources say there is no permanent free S3 allowance after the credits. The official price table did not render, so the S3 storage rate is third-party only. Cost is small at our scale, but it is not zero. |
| **D. Local disk in dev, object storage in production** | Rejected. Two code paths, so dev does not test what production runs. |
| **E. Bytes in PostgreSQL, behind an interface** | **Accepted for v1.** Points 1 to 3 above. |

MinIO was also considered in the dev role and rejected: its repository was archived and set read-only on 25 April 2026, and its README says it is no longer maintained (checked 2026-10-23).

## Decision

### 1. Bytes live in a tenant-owned table, `document_blobs`

- One row per document version: `(tenant_id, version_id)` is the primary key, and `version_id` is a composite foreign key to `document_versions (tenant_id, id)`, the same pattern as every other tenant table (ADR 0011).
- `content bytea NOT NULL`, `byte_size integer` with CHECK `byte_size BETWEEN 1 AND 10485760` (10 MiB, SPEC §6.1) and `byte_size = octet_length(content)`.
- **No `storage_key` column on this table (amended 2026-10-23, decision P2 b).** The key is read from the version row, which already holds it with its tenant-prefix CHECK. The key stays the one logical identifier, so a later move to object storage changes one adapter and no other code, and two copies of the key cannot drift.
- RLS enabled and forced, with the same tenant policies as the other tenant tables. The table has no policy for any role other than the ones in §3.
- Normal document queries never read this table. Listing, status and retrieval for answers use `documents` and `document_versions`, which do not carry the bytes.

### 2. An `ObjectStore` interface, with two implementations

```python
class ObjectStore(Protocol):
    async def put(self, session: AsyncSession, version: VersionRef, data: bytes, *, storage_key: str) -> None: ...
    async def get(self, session: AsyncSession, version: VersionRef) -> bytes: ...
    async def exists(self, session: AsyncSession, version: VersionRef) -> bool: ...
    async def delete(self, session: AsyncSession, version: VersionRef) -> int: ...
```

- **`PostgresObjectStore`** writes through the caller's tenant transaction (`begin_tenant_transaction` for the web app, `begin_worker_transaction` for the worker). It never opens its own connection, so the bytes commit or roll back with the version row and the ingestion job (point 1 above).
- **`InMemoryObjectStore`** is the fake for unit tests. It enforces the same key rules and size limit, so unit tests cannot pass with input the database would refuse.
- `delete` returns the number of rows removed. A missing row is success (ADR 0007). The rowcount rule (ADR 0011, `expect_rowcount`) is applied to the expected range `{0, 1}`, not to a single value, so a delete that removes two rows is still an error.

### 3. Grants and policies (least privilege, enforced by the database)

| Role | Table `document_blobs` | Why |
|---|---|---|
| `app_user` | **INSERT only**, `WITH CHECK` tenant scoped **and the version must be `queued`** (amended, see below) | The upload writes the bytes. No SELECT, UPDATE or DELETE: the web app never reads or changes bytes in v1. |
| `worker_user` | **SELECT and DELETE**, tenant scoped, by version status (amended, see below) | SELECT for text extraction (`queued`, `processing`) and for the purge (`purging`). DELETE only for `purging` (ADR 0007, step 3). |

- **Answer to the download question:** `app_user` gets **no SELECT** in v1. SPEC §6.1 has no requirement to serve the original file. If a "view original" endpoint is added later, it needs a new decision and a new migration granting SELECT on `content`.
- The worker's SELECT and DELETE are restricted further by policy, not by grant (amendment below).
- The grants are recorded in `tests/db/expected_grants.json` (schema `grounded`). The catalog guard fails on any grant that is not in that list.

### 4. Size, encryption and backups (recorded for the hosting checklist)

- **Database size grows with the files.** Each file is up to 10 MiB, so the database size is the sum of the stored versions, plus the retention window (ADR 0007). The hosting checklist must check the provider's storage limit, and record it in RUNBOOK.
- **Backups contain the bytes.** A deleted document's bytes stay in backups until the backup expires (ADR 0007, SECURITY R3). With bytes in the database, the backup window now holds document content as well as rows. The hosting checklist must record the backup window and its size.
- **Encryption at rest is the host's responsibility.** The application does not encrypt the bytes. The host's disk encryption must be confirmed when a host is chosen, and recorded in SECURITY.md. Application-level encryption is a later option (ADR amendment).

## Consequences

- **Positive:** no orphaned objects and no dangling keys. Bytes, row and job commit or roll back together. No storage credentials exist, so R5 is closed for v1 (the worker can delete only through RLS, and only in its tenant). No second service in dev, CI or production.
- **Positive:** the same code path runs in dev, CI and production.
- **Negative:** the database grows, and backups grow with it (point 4).
- **Negative:** a large table in the same database as tenant rows. Loading a version's bytes is one primary-key lookup, but a bulk read of many files must be kept out of the request path (ingestion runs in the worker).
- **Negative:** a future move to object storage is a migration with a copy step. The interface and the logical key make it a bounded change.

## Scale-up path

When document volume or backup size justifies it, the first object store to evaluate is **Backblaze B2**, with `storage_key` as the object key, its application keys (`writeFiles` and `readFiles` for the web app, `readFiles` and `deleteFiles` for the worker, both restricted to one bucket and the `tenant/` prefix), and its server-side encryption on by default. The move is: copy bytes out, switch the adapter, drop the table. That is a new ADR when it happens.

## Amendment (2026-10-23): status-scoped policies, the storage key, and a DELETE finding

Decided by the owner in the Step 2a review.

**P1: the worker reads by status, not by tenant.**
- `worker_user` SELECT on `document_blobs` is allowed for versions in `queued`, `processing` or `purging`. It is tenant scoped. Ingestion reads `queued` and `processing`. The purge reads `purging`, the bytes it is about to delete.
- `worker_user` DELETE is allowed only for versions in `purging`.
- The bytes of `ready`, `failed` and `retired` versions are unreadable to the worker.
- A re-ingestion of `ready` documents (for example after an embedding-model change) needs a new migration that widens SELECT. It is never loosened silently.

**Finding: DELETE needs SELECT visibility.** PostgreSQL requires a row to be visible through a SELECT policy before DELETE (or UPDATE) can act on it. With SELECT limited to `queued` and `processing`, the purge's DELETE on a `purging` blob matched zero rows and raised no error, so the bytes would have stayed. Two tests caught it (`test_worker_deletes_bytes_of_a_purging_version` and `test_the_purge_order_removes_bytes_before_the_version_row`). The fix widens SELECT to `purging`, the one extra status the purge needs, and nothing more.

**P2: no `storage_key` column.** The key is read from `document_versions.storage_key`, as point 1 now says.

**App-side upload condition.** `app_user` INSERT is allowed only for a version in status `queued`. A `ready`, `processing`, `failed`, `retired` or `purging` version cannot take new bytes. This is an addition beyond the original plan, and the owner approved it.

## Related decisions

- ADR 0007 (retention, purge steps, idempotent delete, backups)
- ADR 0011 (composite foreign keys, `expect_rowcount`)
- ADR 0015 (outbox, `worker_user` for purge)
- ADR 0016 (queue grants, catalog guard for queue)
- ADR 0017 (worker credentials split, begin_worker_transaction)
- SECURITY R3 (backups), R5 (worker deletes stored files)

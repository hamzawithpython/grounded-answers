# ADR 0022: Ingestion Worker: Leases, Fencing, Retries and the Embedding Model Files

**Status:** Accepted (Session 4, Step 6). The stalled-job retry is not built yet (see Consequences).

**Date:** 2026-10-23

**Deciders:** Hamza Asif

## Context

An uploaded document version (ADR 0019) is ingested by a background job (ADR 0015). The job extracts the text in a sandbox (ADR 0020), chunks it (ADR 0021, strategy S1), embeds each chunk with the pinned model (ADR 0021), and writes the chunks and the version status in the database.

Jobs are delivered at least once, so the same version can be attempted twice. A worker can also stop in the middle, or pause long enough that another attempt takes over. The write path must give exactly one set of chunks and one final status, whatever happens.

## Decisions

### D1. Lease and fencing token

- A claim sets `status = processing`, `processing_started_at` (a database timestamp) and `processing_token` (a new uuid per attempt).
- A process holds the version only while its lease is fresh. The lease length is a worker setting, `WORKER_PROCESSING_LEASE_SECONDS`, default 1200 s, bounded to 120 to 7200 s.
- After the lease expires, another attempt may take over and sets a new token.
- The final write (ready, or failed) requires the token it claimed with. A worker that lost its lease gets zero rows and rolls back everything.
- The worker derives its token from the Procrastinate job id (UUID v5). A retry of the same job keeps its token, so it resumes its own lease. Any other job for the same version has a different token and waits.

### D2. One transaction for the write

Extraction and embedding run first, outside any transaction. Then one transaction checks the fencing token, deletes the version's chunks, inserts the new chunks in batches of 100, marks the version ready, sets the document's current version (if none), and writes one audit row. A crash rolls back all of it.

### D3. Grants and policies (approved)

Migration `0009_ingestion_lease` adds two columns and one check, and gives `worker_user` UPDATE on three columns:

- `document_versions.failure_reason`, `processing_started_at`, `processing_token` (plus `status`, already granted).

The policies on `document_versions` and `documents`, as they stand after this migration:

| Policy | Table | Operation | USING | WITH CHECK |
|---|---|---|---|---|
| `worker_versions_claim` | document_versions | UPDATE (worker_user) | tenant, `status = 'queued'` | tenant, `status = 'processing'` |
| `worker_versions_process` | document_versions | UPDATE (worker_user) | tenant, `status = 'processing'` | tenant, `status IN ('processing','ready','failed')` |
| `worker_documents_set_current` | documents | UPDATE (worker_user) | tenant, live document, `current_version_id IS NULL` | tenant, live document, and the version is `ready` and belongs to the document |

**Deviation from the approved wording, stated plainly.** The approval said the UPDATE policy is limited to versions currently in `processing`. The claim changes a `queued` row to `processing`, so a rule limited to `processing` cannot allow the claim. The claim has its own policy above. The finish and failure writes are still limited to rows in `processing`.

The existing purge policies on `document_versions` (`worker_versions_update`, `worker_versions_delete`) are unchanged.

### D4. Failure reasons: a fifth, `embedding` (amends ADR 0020)

ADR 0020 mapped every extraction failure to one of four reasons. The embedding model can fail on a document's text, and no existing reason describes that. The fifth reason is `embedding`. The stderr rule of ADR 0020 still holds: the model's error message is never stored or logged, only its exception class name. ADR 0020 has an amendment that lists the five reasons.

### D5. Last attempt, and the gap

- Transient errors (database connection or timeout, lease held by another attempt) are retried with exponential waits, up to 5 attempts. Only the listed exception types are retried.
- On the last attempt, a transient error marks the version `failed` with `retries_exhausted`. This is a generic reason of its own, not an extraction reason: an admin must be able to tell a bad document from an outage (decision made in review, 2026-10-23).
- **The gap:** a worker that is killed hard (for example, a process kill or a power loss) never reaches the last-attempt code. Its version stays `processing`, with no job to retry it, until its lease expires and someone enqueues a new job. A periodic stalled-job retry closes this. It needs a cross-tenant scan of versions in `processing`, and `worker_user` is tenant-scoped (ADR 0017), so the scan needs a security-definer function, which is a new decision. It is the first item of Step 7 in `docs/PROGRESS.md`.

### D6. Model loaded once per worker process

- The worker loads the model at startup and refuses to start if a pinned file is missing or does not match its hash (`PinnedEmbedder`, `verify_model_dir`).
- Inference runs in batches of 8, in a thread, so the event loop stays responsive.
- Production runs offline. The model is fetched once, at build or deploy, by `python -m grounded_answers.embedding.fetch <cache>`.

### D7. Model files: pinned, hashed, cached

- The revision is pinned to a Hugging Face commit (`MODEL_REVISION`). Each file is pinned by SHA-256 and size (`PINNED_FILES`).
- The fetch step downloads a file only when it is missing. It hashes the download before keeping it. A file that is already in the cache is never replaced: it is checked, and a mismatch fails loudly.
- CI restores a cache keyed by the revision, then runs the fetch step, so a corrupted or tampered cache fails in CI.
- The runtime is onnxruntime and tokenizers, used directly (ADR 0021, amendment). It loads the verified directory and downloads nothing.

### D8. Transition guard (approved in review)

The policies in D3 cannot express "from queued to processing" or "from processing to ready": a WITH CHECK sees only the new row, and permissive policies are combined with OR. Without a control, a worker could move a queued version straight to ready (a test showed this). So the worker's status changes are enforced by a BEFORE UPDATE trigger, `document_versions_worker_transitions` (migration `0009`):

- It applies only when `current_user = 'worker_user'`. Other roles are not affected by it.
- It allows exactly these changes: queued to processing, processing to ready, processing to failed, retired to purging (the purge step).
- Any other change by the worker raises `insufficient_privilege` with the message "worker_user may not move a version from X to Y".
- It follows the pattern of the `updated_at` function: `search_path` pinned, EXECUTE revoked from PUBLIC.

For Step 7, the guard is extended to a per-role table of allowed transitions, so that app_user's replace and delete changes are guarded in the same way.
## Dependencies (amended 2026-10-23)

`onnxruntime==1.31.0` (MIT, uploaded 2026-10-09), `tokenizers==0.23.3` (Apache-2.0, uploaded 2026-10-09) and `numpy==2.5.4` (BSD-3-Clause), all used directly. `fastembed` was removed: it caps pillow below 13.0, and pillow 12.3.0 carries 35 advisories that 13.0.0 fixes (pip-audit). We never process images. `huggingface-hub` is pinned to 1.33.0 through `[tool.uv] constraint-dependencies`: `tokenizers` pulls it in, the project does not use it, and 2.3.0 was uploaded on 2026-10-23, inside the one-week cooldown (ADR 0005).

## Embedding text (a difference from the evaluation)

The evaluation (ADR 0021, `docs/EVAL.md`) embedded chunks with the document identity read from the header (for example `IFL-FIN-007 v2.0`). Production has no header parser. It embeds each chunk with the document title and version number (`Expense Policy v2 | 3.2 Probation`). The model, the prefix and the chunks are the same, but the text differs, so the evaluation's numbers do not directly describe production. A re-run of the evaluation on production embedding text is part of the Session 5 work.

## Tests

- Pipeline (real database, fake embedder): markdown, PDF and DOCX versions become ready with chunks and model metadata; claim and resume; a fresh lease makes another job wait; an expired lease is taken over; ready, failed, retired and purging versions are skipped; a version outside the tenant is a poison job; a crashed write leaves no chunks, and the retry leaves one set; an older worker that lost its lease writes nothing; a lost lease cannot mark the version failed; each failure reason; a missing blob; an embedding failure; no document text in logs; the registered task gives up only on its last attempt.
- Units: the chunker (limits, tables whole, clause labels, body limit); verification refuses a missing or changed file; fetch refuses a download that does not match its pin and keeps nothing; the cache is checked, not replaced; only transient errors are retried; the lease bound.
- One real-model test (DB job, cache in CI): the pinned model loads offline, gives normalised 384-dimensional vectors, and ranks the relevant passage first.

## Consequences

- **Positive:** a crash or a lost lease can never leave a half-written set of chunks or a second one. The model is reproducible and checked.
- **Negative:** one worker process needs about 1 GB of RAM for the model (ADR 0021). A hard-killed worker leaves a version in `processing` until Step 7 (D5).
- **Negative:** a version that fails after its retries stays `failed` until someone re-enqueues it. Re-enqueueing is manual until Step 7.
- **Negative:** the embedding text differs from the evaluated text (see above).
- **Operational:** the worker needs `WORKER_EMBEDDING_MODEL_DIR` set (docs/RUNBOOK.md). Upload stays off (`UPLOAD_ENABLED`) until Step 8, because a running worker processes any queued job.

## Related decisions

- ADR 0015 (Procrastinate, at-least-once delivery, retries)
- ADR 0017 (worker credentials, log redaction)
- ADR 0018 (document bytes, and the migration rule for widened access)
- ADR 0019 (upload endpoint)
- ADR 0020 (extraction, amended here: reason list)
- ADR 0021 (embedding model and chunking)

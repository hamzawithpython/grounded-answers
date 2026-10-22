# ADR 0019: Document Upload Endpoint

**Status:** Accepted

**Date:** 2026-10-23

**Deciders:** Hamza Asif

## Context

SPEC §6.1 requires document upload: PDF, DOCX, TXT and Markdown, at most 10 MiB, with the type checked from the file's content. ARCHITECTURE §3 and §5 define the storage (ADR 0018) and the lifecycle (ADR 0007). This ADR records how the upload endpoint, `POST /documents`, meets those rules, and the decisions made in the Step 3 review.

## Decisions

### D1. Multipart parsing: `python-multipart` (new dependency)

- Added: `python-multipart` 0.0.32 (PyPI, checked 2026-10-23). **Licence:** Apache-2.0. It is FastAPI's standard multipart parser.
- **Alternatives considered:** our own streaming parser (no dependency, but a security-sensitive parser to write and test), or a raw body with the filename in a header (simplest, but not the multipart API the SPEC describes).
- **Caveat recorded:** a large file part is spooled to a temporary file on the host before the endpoint runs. The byte cap (D2) stops the body first, and the temporary file is removed after the request.

### D2. The size cap runs while the body streams, before any parser

- Parsers run before the endpoint, so a check in the endpoint is too late. `UploadBodyLimit` is an ASGI middleware on `POST /documents` only.
- A `Content-Length` above the cap is refused at once, without reading the body.
- Otherwise each chunk is counted as it arrives, chunked bodies included. Past the cap the request is stopped with 413.
- **Cap:** the body is limited to 10 MiB plus 64 KiB of multipart framing. The file itself is limited to exactly 10 MiB by the endpoint.
- **Form limits:** one file, at most 8 fields, and 1 KiB for each non-file field.

### D3. Type from the bytes, extension must agree

- **PDF:** starts with `%PDF-`, and the extension is `.pdf`.
- **DOCX:** a ZIP archive containing `[Content_Types].xml` and `word/document.xml`. ZIP limits: at most 2,000 entries, at most 50 MiB declared unpacked in total, at most 20 MiB for any one entry. Encrypted entries are refused. Nothing is extracted to disk. The one entry read is read through a byte counter, because a header can declare a small size for a large stream.
- **TXT and MD:** valid UTF-8, no NUL byte, and not the magic of another type. A PDF renamed `.txt` is refused, even though it may decode as text.
- **The client's `Content-Type` header is ignored.** Anything else gets 415.

### D4. Filename: display only, sanitised

- Only the last path segment is kept (both `/` and `\` are separators).
- Control characters, NUL, bidirectional controls (for example U+202E) and zero-width characters are removed.
- Tabs and line breaks become spaces, before control characters are removed, so words are not joined.
- Names are normalised to Unicode NFC. Unicode letters are kept.
- The name is limited to 255 characters. An empty name, `.` or `..` becomes `document`.
- The name is never used as a storage key or to choose a type.

### D5. Duplicates

- **409** with the existing `document_id` (`{"detail": ..., "document_id": ...}`).
- Only live versions count: a document that is not deleted, with a version in `queued`, `processing` or `ready`. A deleted document does not block its file from being uploaded again, and a failed upload does not block a retry.
- A transaction-scoped advisory lock on `(tenant, sha256)` makes two identical uploads at the same moment run one at a time.

### D6. The ingestion job

- **Task name:** `grounded.ingest_document` (`jobs/tasks.py`). It is on the enqueue allowlist.
- **Arguments:** `tenant_id`, `document_id`, `version_id`. All three are UUIDs (ADR 0015, decision 3). The tenant id is passed because the worker treats it as untrusted (decision 9).
- **The endpoint is off by default.** `UPLOAD_ENABLED` defaults to `false`, and the endpoint answers 404 while it is off. The switch stays off until the worker runs the ingestion task (Step 6), so no job can wait in the queue with nothing to run it.

### D7. Audit

- Action `document.upload`, target `document`, with the uploader as the actor.
- Detail: `size_bytes` and the content label only. No title, no filename, no hash.

### D8. Rate limit

- Per tenant: 60 attempts per hour. Per user: 20 attempts per hour.
- An attempt counts when it is made, including one later refused for its type or size, so a client cannot probe the rules for free.
- Valkey unavailable: **503**. This matches the login decision (ADR 0014, fail closed).
- Keys hold an HMAC of the ids, and expire with their window.

### D9. Responses

- Errors use the existing shape, `{"detail": "..."}`. The 409 also carries `document_id`. Stable error codes are a later change, applied everywhere at once.
- **201:** `{"document_id", "version_id", "status": "queued"}`.
- Messages are generic. The parser's text, the filename and the driver's text never reach the client.

## Findings from the build (2026-10-23)

- **Starlette turns form errors into `HTTPException`.** Inside an app, `request.form()` raises `starlette.exceptions.HTTPException`, with the parser's message as its detail. Catching it needs the Starlette class. The FastAPI subclass does not match it, and an early version of the endpoint leaked the parser message as a result. A test now checks the generic message.
- **FastAPI's `UploadFile` is a subclass of Starlette's.** The form returns Starlette's class, so checks must use Starlette's `UploadFile`.
- **The Procrastinate delete trigger needs `queue` on the session's search path.** It names `procrastinate_periodic_defers` without a schema. The app and worker roles have it from the bootstrap. A session without it fails when it deletes a job. The test cleanup sets the path. The remove-old-jobs task (Step 7) runs as `worker_user`, so it is unaffected.
- **`zipfile` clears the encryption bit on write.** The encrypted-entry test sets it in the central directory.

## Consequences

- **Positive:** a large upload is refused while it streams, whatever the client claims. A refused upload leaves no document, bytes, audit row or job. A file's type comes from its content, not from its label.
- **Positive:** a deleted document's file can be uploaded again, and a failed upload can be retried.
- **Negative:** the parser may spool a file to disk before the cap is reached. The cap bounds how much is written.
- **Negative:** the endpoint stays off until Step 6.

## Related decisions

- ADR 0007 (retention, deleted documents)
- ADR 0014 (login limits and fail-closed pattern)
- ADR 0015 (Procrastinate, ids-only arguments, tenant untrusted)
- ADR 0017 (worker credentials, enqueue, log redaction)
- ADR 0018 (document bytes in PostgreSQL)

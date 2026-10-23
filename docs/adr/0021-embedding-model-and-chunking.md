# ADR 0021: Embedding Model and Chunking Strategy

**Status:** Accepted (model and chunking). The chunks table migration is planned and not yet written.

**Date:** 2026-10-23

**Deciders:** Hamza Asif

## Context

Session 5 answers questions from the ingested documents. Each document's text is split into chunks, each chunk is embedded as a vector, and a question is matched against the chunks. The `chunks` table must have a fixed vector dimension, so the model is decided now. The web app must also load the model to embed each question, and the worker must load it to embed chunks. Both run on a small Linux host, so memory limits what can be hosted.

Documents are confidential tenant data (SPEC §6.1). Any hosted embedding service would receive them, which rules out a service whose free tier uses submitted content to improve its products.

Urdu answers are planned for v2 (SPEC §6.2). Some users may ask in Roman Urdu or in Urdu script.

## Decisions

### 1. Embedding model: `intfloat/multilingual-e5-small`

- **Licence:** MIT (model card, checked 2026-10-23).
- **Dimension:** 384. Well under pgvector's HNSW limit of 2,000 for `vector` (pgvector README, checked 2026-10-23).
- **Prefixes:** `passage: ` on every chunk, `query: ` on every question. Both are required by the model card and were used in the evaluation.
- **Runtime:** onnxruntime 1.31.0 (MIT) runs the model, and tokenizers 0.23.3 (Apache-2.0) splits the text. Both are used directly (amendment below). The model is the official `intfloat/multilingual-e5-small`, from its `onnx/model.onnx` (fp32, 448.5 MiB). The model download is about 0.47 GB.
- **Dependency:** resolved by the amendment below. No fastembed.

### 2. Chunking: section-aware, about 1,200 characters, tables whole (strategy S1)

- A chunk is a piece of one numbered section, split at line breaks, at most about 1,200 characters.
- Each table is one chunk. Tables are never split.
- Every chunk stores: document ID and version (from the document header, not the filename), section number, heading, the clause labels it contains (for citations such as §3.2.2), and the text.
- Embedding text is the chunk text with the `passage: ` prefix. The heading prefix (document ID, version, section, heading) is part of the embedded text.

### 3. Store both a vector and a full-text index

The chunks table stores `embedding vector(384)` and a generated `tsvector`. Whether retrieval uses vector search, keyword search, a fusion of the two, or a reranker is decided in Session 5, using the same evaluation. Storing both keeps that choice open at no extra migration cost.

**Input to the Session 5 decision:** the `tsvector` uses PostgreSQL's English configuration (stemming and stop words for English). Keyword search with that configuration will not match Roman Urdu or Urdu script. Keyword-only retrieval cannot answer those questions, so Session 5 must not rely on it for them.

### 4. Hosting requirement: about 1 GB RAM per process

Measured in one process per model on Windows (peak working set):

| Use | Measured | Requirement |
|---|---|---|
| Web app loads the model to embed a question | 923 MiB | at least 1 GB free for the web process |
| Worker loads the model to embed chunks | about 1,050 MiB peak | at least 1 GB free for the worker process |

**Hosting requirement:** the web app and the worker each need about 1 GB of RAM. Free tiers with 512 MB cannot host this model. Any future host must be checked against this number before it is chosen.

### 5. ONNX memory tuning: later

Thread count and the ONNX memory arena can reduce the peak. This is not done now. It is a later optimisation, and any change must be measured again with the same method.

### 6. Re-embedding plan: model and version stored per chunk

- Every chunk row stores `embedding_model` (for example `intfloat/multilingual-e5-small`) and `embedding_model_version` (the Hugging Face commit hash of the model files, pinned).
- A change of model, or of model version, is a deliberate migration. It creates a new vector column or table, re-embeds every chunk, then switches reads. This follows ADR 0018, which requires a new migration for any change that widens access to stored data (see its re-ingestion note).
- Queries use only chunks whose `embedding_model` matches the model the app loads. A mismatch is an error, not a silent fallback.

## Evidence

Method and full results are in `docs/EVAL.md`. Summary:

**Retrieval on the owner's 40 answerable questions (45 expected clause pairs):**

| Model | Chunking | Vector R@5 | Vector MRR |
|---|---|---|---|
| bge-small-en-v1.5 (English only) | S1 | 0.80 | 0.637 |
| nomic-embed-text-v1.5 (English only) | S2 | 0.80 | 0.661 |
| multilingual-e5-small | S1 | 0.78 | 0.633 |
| paraphrase-multilingual-MiniLM-L12-v2 | S2 | 0.69 | 0.640 |

Keyword search (BM25 with stemming) scores 0.73 R@5 at best. The differences among the top three models are within the sampling noise of 45 pairs. Clause coverage is 100%.

**Urdu mini-test (6 questions I wrote, not reviewed by a native speaker, over HR-001 and HR-002):**

| Model | Roman Urdu R@5 / MRR | Urdu script R@5 / MRR |
|---|---|---|
| bge-small-en-v1.5 | 0.67 / 0.236 | 0.00 / 0.013 |
| multilingual-e5-small | 0.67 / 0.505 | 1.00 / 0.500 |

Roman Urdu is only partly handled: e5-small finds two of three questions in the top 5, and the third (late arrival, "dair se aane") is not found in the top 5. The sample is too small to call this a result. Native-speaker questions should be added before v2 planning.

**Query cost:** about 26 ms mean per question for e5-small. Its load time on this machine was 54 s, including the first download.

## Rejected options

- **bge-small-en-v1.5:** English only. Equal on English retrieval, and it does not answer the Urdu test (0.00 R@5 for Urdu script).
- **nomic-embed-text-v1.5:** English only, about 0.7 GB at query time, and 105 ms mean latency per query.
- **multilingual-e5-large:** the largest multilingual option. It did not run to completion on this machine (memory). At about 1.75 GB peak it does not fit the hosting requirement in decision 4.
- **paraphrase-multilingual-MiniLM-L12-v2:** multilingual, but the weakest vector result (0.69 R@5 at S1).
- **Hosted embeddings (Gemini Embedding 2):** the free tier uses submitted content to improve Google's products (pricing page, checked 2026-10-23). Tenant documents would leave our system. The paid tier is $0.20 per 1M tokens, which breaks the zero-cost constraint. Not used.

## Consequences

- **Positive:** MIT licence, 384 dimensions, multilingual model, measured query latency of about 26 ms.
- **Negative:** about 1 GB RAM per process. Free hosting tiers with 512 MB cannot run the app or the worker. The model file is a 0.47 GB download.
- **Negative:** the evaluation is small (45 expected pairs). Model choice may need revisiting when more real questions exist.
- **Negative:** the Urdu examples are written by the assistant and not reviewed by a native speaker.
- **Dependency:** onnxruntime and tokenizers, see the amendment below.
- **pgvector is not enabled yet.** No migration or bootstrap script creates the `vector` extension (checked 2026-10-23: the only `CREATE EXTENSION` in the repository is for `plpgsql`). The container image ships pgvector, but the extension must be created in each database. The bootstrap SQL will run `CREATE EXTENSION IF NOT EXISTS vector` (idempotent), and "enable pgvector" is on the hosting checklist (RUNBOOK).
- **Model files are fixed at build time.** Production runs offline: the pinned model is fetched once at build or deploy into a fixed cache directory, never at startup. A startup test fails with a clear message if the files are missing or do not match the pinned revision.

## Related decisions

- ADR 0018 (document bytes in PostgreSQL, and the migration rule for widened access)
- ADR 0020 (text extraction, which produces the sections this ADR chunks)

## Amendment (2026-10-23): onnxruntime and tokenizers instead of fastembed

**Why.** fastembed 0.9.0, the latest release, caps pillow below 13.0. Pillow 12.3.0, which fastembed brought in, carries 35 advisories, all fixed in 13.0.0 (pip-audit in CI). We never process images, so pillow is not needed, but the CI check is strict and cannot be disabled.

**What changed.** The model runs through onnxruntime 1.31.0 (MIT, uploaded 2026-10-09). The text is split by tokenizers 0.23.3 (Apache-2.0, uploaded 2026-10-09). numpy 2.5.4 (BSD-3-Clause) is declared directly. fastembed is removed. The loader keeps the same steps as the model card: the "passage: " or "query: " prefix, truncation at 512 tokens, attention-masked mean pooling, and L2 normalisation.

**Equivalence.** Measured on the 829 chunks of the evaluation and the 40 questions, against fastembed's vectors for the same texts: minimum cosine similarity 0.99999988, mean 1.0. Retrieval on the owner's 40 questions is unchanged: S1 recall@5 0.778 and MRR 0.633, S2 0.778 and 0.605, S3 0.756 and 0.587.

**Session settings** (measured, `docs/EVAL.md`, "Runtime change"): 2 intra-op threads, one inter-op thread, the memory arena off, memory pattern off. On the S1 chunks, two threads with the arena off was the fastest configuration. The peak working set was the same for every configuration tried.

**Cost.** Peak working set, one process: about 972 MiB after loading the model and during embedding (fastembed: 923 MiB after load, 1,048 MiB at peak). The ~1 GB per process requirement in decision 4 still holds.
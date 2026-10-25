# Evaluation

How we measure retrieval quality, and the latest results. Decisions based on these numbers are recorded in ADRs (latest: ADR 0021).

## Method (embedding and chunking choice, Session 4, 2026-10-23)

**Data.** The eight demo documents in `demo-data/indus-freight/upload`, extracted with the project's extraction code (ADR 0020). Each document's identity (ID and version) is read from its header text, not its filename. The 50 questions in `demo-data/indus-freight/eval-questions.json`: 40 are answerable, 10 are not. Only answerable questions are scored.

**Expected answers.** Each answerable question names one or more sources as (document, version, clause). A question with two sources gives two pairs: 40 questions, 45 pairs. A question that cites `v1.0` of IFL-FIN-007 is scored against the retired version. Under the owner's rule, a hit from a retired version counts as a miss, so one pair (q045, v1.0 clause 6.1) is always a miss.

**Hit.** A retrieved chunk is a hit for a pair when it comes from the expected document and version, is not from a retired version, and contains the expected clause label as a whole label (`3.2` does not match `3.2.2`). Chunks carry their clause labels, so a clause is found when its label appears in the chunk.

**Metrics.** Recall@5 is the share of pairs with a hit in the top 5. MRR (mean reciprocal rank) is the average of 1/rank of the first hit, with 0 when there is none.

**Chunking strategies.**
- S1: section-aware, about 1,200 characters, tables whole (190 chunks).
- S2: section-aware, about 400 characters, tables whole (384 chunks).
- S3: fixed 600-character windows with 100 overlap, tables whole (255 chunks). The window's section is the section where it starts.

**Rankers.** Keyword search: BM25 with English stemming and stop words (a Postgres-FTS-style baseline). Vector search: cosine similarity over the model's embeddings. Hybrid: reciprocal rank fusion (k = 60) of the two.

**Query prefixes.** Chunks are embedded with the model's document prefix and questions with its query prefix: `passage:` and `query:` for e5 models; `search_document:` and `search_query:` for nomic; a query instruction for bge.

**Resources.** Each model runs in its own process, one at a time. RAM is the peak working set on Windows. Query latency is each question embedded alone, after one warm-up.

## Results: 40 answerable questions, 45 pairs

Clause coverage (share of expected clause labels found in some chunk's clause list): 100% for S1, S2 and S3.

Keyword (BM25):

| Chunking | R@5 | MRR |
|---|---|---|
| S1 | 0.73 | 0.598 |
| S2 | 0.73 | 0.556 |
| S3 | 0.76 | 0.591 |

Vector and hybrid:

| Model | Dims | Chunking | Vector R@5 | Vector MRR | Hybrid R@5 | Hybrid MRR |
|---|---|---|---|---|---|---|
| bge-small-en-v1.5 | 384 | S1 | 0.80 | 0.637 | 0.78 | 0.615 |
| bge-small-en-v1.5 | 384 | S2 | 0.73 | 0.617 | 0.78 | 0.586 |
| bge-small-en-v1.5 | 384 | S3 | 0.73 | 0.539 | 0.78 | 0.624 |
| nomic-embed-text-v1.5 | 768 | S1 | 0.80 | 0.651 | 0.76 | 0.634 |
| nomic-embed-text-v1.5 | 768 | S2 | 0.80 | 0.661 | 0.80 | 0.601 |
| nomic-embed-text-v1.5 | 768 | S3 | 0.73 | 0.524 | 0.76 | 0.620 |
| paraphrase-multilingual-MiniLM-L12-v2 | 384 | S1 | 0.69 | 0.542 | 0.78 | 0.562 |
| paraphrase-multilingual-MiniLM-L12-v2 | 384 | S2 | 0.69 | 0.640 | 0.73 | 0.590 |
| paraphrase-multilingual-MiniLM-L12-v2 | 384 | S3 | 0.67 | 0.473 | 0.73 | 0.567 |
| multilingual-e5-small | 384 | S1 | 0.78 | 0.633 | 0.76 | 0.656 |
| multilingual-e5-small | 384 | S2 | 0.78 | 0.605 | 0.80 | 0.600 |
| multilingual-e5-small | 384 | S3 | 0.76 | 0.587 | 0.82 | 0.640 |
| multilingual-e5-large | 1024 | all | not run (process exited with code 1, likely out of memory) | | | |

**Reading the results.** 45 pairs means one pair moves recall by 0.02. Differences of 0.02 to 0.05 in MRR are within noise.

Resources and query cost:

| Model | Download | RAM after load | RAM while embedding one query | Query mean | Query p95 | Peak, all chunks |
|---|---|---|---|---|---|---|
| bge-small-en-v1.5 | 0.067 GB | 229 MiB | 229 MiB | 27 ms | 47 ms | 364 MiB |
| paraphrase-multilingual-MiniLM-L12-v2 | 0.22 GB | 613 MiB | 613 MiB | 27 ms | 47 ms | 662 MiB |
| multilingual-e5-small | 0.47 GB | 923 MiB | 923 MiB | 26 ms | 46 ms | 1,048 MiB |
| nomic-embed-text-v1.5 | 0.52 GB | 696 MiB | 696 MiB | 105 ms | 156 ms | 1,457 MiB |

## Results: Urdu mini-test (6 questions)

Six questions over HR-001 and HR-002, three in Roman Urdu and three in Urdu script, written by the assistant and **not reviewed by a native speaker**. Chunks are the S1 chunks.

| Model | Roman Urdu R@5 / MRR | Urdu script R@5 / MRR | All six R@5 / MRR |
|---|---|---|---|
| bge-small-en-v1.5 | 0.67 / 0.236 | 0.00 / 0.013 | 0.33 / 0.124 |
| multilingual-e5-small | 0.67 / 0.505 | 1.00 / 0.500 | 0.83 / 0.502 |

Roman Urdu: "dair se aane par kya hota hai?" (late arrival, 4.4) is not in the top 5 for either model. The sample is too small to draw a conclusion. Add native-speaker questions before the v2 Urdu decision.

## Limits

- 45 expected pairs from one owner-written question set. Small differences are noise.
- Retired-version pairs are always misses by rule.
- The question set cites five documents that the demo set now names by their header IDs. Re-check the mapping if the demo set changes.
- The Urdu questions are not native-reviewed.
- The evaluation scripts are scratch work outside the repository. They are not committed. Moving them into the repository is a later decision.

## Reproduce

Scratch scripts: extraction (`extract_all.py`), chunking (`chunks.py`), embedding (`embed_model.py`, one model per process), scoring (`evaluate.py`), and the Urdu mini-test (`urdu_test.py`). Model downloads and total memory are listed above. Run one model at a time.

## Runtime change: onnxruntime and tokenizers instead of fastembed (2026-10-23)

fastembed 0.9.0 caps pillow below 13.0, and pillow 12.3.0 carries 35 advisories that 13.0.0 fixes (ADR 0021, amendment). The embedder now runs the same model through onnxruntime 1.31.0 and tokenizers 0.23.3, with the same prefixes, truncation, mean pooling and normalisation.

### Equivalence against fastembed

Reference vectors from fastembed 0.9.0, for the same texts (829 chunks, 40 questions).

| Set | n | Min cosine | Mean cosine | Max cosine |
|---|---|---|---|---|
| Queries | 40 | 1.0 | 1.0 | 1.00000012 |
| Passages S1 | 190 | 0.99999988 | 1.0 | 1.00000024 |
| Passages S2 | 384 | 0.99999988 | 1.0 | 1.00000024 |
| Passages S3 | 255 | 0.99999988 | 1.0 | 1.00000024 |

Retrieval on the same 45 expected pairs is identical to the fastembed figures: S1 recall@5 0.778, MRR 0.633; S2 0.778 and 0.605; S3 0.756 and 0.587.

### Memory and latency in the same conditions (2026-10-23)

One process each. Each process loads the corpus first, then the model, then embeds the 40 questions one at a time after one warm-up.

| Embedder | Peak working set after load (MiB) | Mean query (ms) | Median (ms) | p95 (ms) | Max (ms) |
|---|---|---|---|---|---|
| New (onnxruntime 1.31.0, tokenizers 0.23.3) | 1,123 | 41.0 | 32 | 78 | 94 |
| fastembed 0.9.0 | 897 | 35.2 | 31 | 63 | 78 |

The new embedder uses 226 MiB (25%) more RAM. Its median query time matches fastembed's, and its tail is slower.

### Investigation (timeboxed, 2026-10-23)

**Padding.** The tokenizer pads each batch to its longest sequence, as fastembed does (checked: two inputs of 27 tokens each after padding). No change is needed.

**Session options.** One process per row, the same 40 questions.

| Threads | Memory arena | Memory pattern | Graph optimisation | Peak after load (MiB) | Mean (ms) | Median (ms) | p95 (ms) |
|---|---|---|---|---|---|---|---|
| 2 | off | off | all (current) | 1,121.9 | 30.5 | 31 | 47 |
| 2 | off | on | all | 1,122.8 | 30.8 | 31 | 63 |
| 2 | on | on | all | 1,123.1 | 20.3 | 16 | 31 |
| default | on | on | all | 1,123.3 | 22.3 | 16 | 47 |
| default | on | on | basic | 1,122.8 | 34.4 | 31 | 63 |
| 2 | off | off | basic | 1,123.0 | 29.7 | 31 | 47 |

Peak RAM stays at about 1,122 MiB whatever the options are. So the gap is not caused by these settings. The memory arena on gives faster single queries (median 16 ms), but slower batch indexing (see below), so the current settings stay.

**Conclusion.** The gap is not closed. The hosting requirement is about 1.1 GB per process, and the figures in this section replace the earlier ones.

**Withdrawn.** Earlier figures in this document and in ADR 0021 compared a new-embedder run with fastembed figures from a different run. That comparison is invalid. The same-condition figures above replace them.

### Batch indexing, session settings (190 S1 chunks, earlier measurement)

| Intra-op threads | Memory arena | Time (s) | Peak working set (MiB) |
|---|---|---|---|
| 1 | off | 89.8 | 1,122.8 |
| **2** | **off** | **69.9** | 1,123.4 |
| 4 | off | 78.8 | 1,123.7 |
| 2 | on | 126.0 | 1,123.3 |

The peak figures in this table include the corpus loaded earlier in the same process. Compare them only with each other. The current setting, 2 threads with the arena off, is the fastest for indexing. The single-query median is faster with the arena on.

## Retrieval evaluation against the database (Session 5, Step 1, 2026-10-26)

The scratch scripts above are replaced by a harness in the repository (`grounded_answers.evaluation`, run with `uv run pytest -m eval -s`, RUNBOOK). It loads the eight demo documents through the real pipeline into the test database (extract, S1 chunks, the pinned model, write, swap), loading IFL-FIN-007 v1.0 first and v2.0 second so the real swap retires v1. It then asks the 50 questions as `app_user`, so row-level security and the current-version rule apply. A document's ID and version come from its header text. The title is the filename, as in the upload endpoint, and the embedding prefix uses it.

**Rankers.** Vector: cosine similarity with pgvector, exact at this size (the HNSW index is not exercised). Keyword: the question's words stemmed by the English configuration and joined with OR, ranked by `ts_rank_cd` (PostgreSQL has no inverse document frequency, so this is not the BM25 of the tables above). Hybrid: reciprocal rank fusion, k = 60. Each list is fetched to depth 50. Ties are broken by the md5 of the chunk body, then `(version_id, position)`.

**Hit rule and metrics.** As in "Method": same document, current version, the named version when the pair names one, and the clause label as a whole label. Recall@5 and MRR are over the 45 pairs (MRR over the top 50). The v1.0 pair of q045 is always a miss, so the ceiling is 44/45 = 0.978.

### Results (k = 5, 40 answerable questions, 45 pairs)

| Ranker | R@5 | MRR | Latency p50 / p95 | Questions with fewer than 5 rows |
|---|---|---|---|---|
| vector | 0.800 (36/45) | 0.636 | 5.8 / 14.1 ms | 0 |
| keyword | 0.733 (33/45) | 0.633 | 7.1 / 21.9 ms | 1 (q015: 4 rows) |
| hybrid | 0.867 (39/45) | 0.742 | 13.9 / 28.8 ms | 0 |

Query embedding: p50 22 ms, p95 34 ms. Chunks of a non-current version returned to `app_user`: 0. Latency is the ranker call inside an open transaction on 157 rows; it says nothing about scale.

By question type (R@5 / MRR, pairs):

| Type | vector | keyword | hybrid |
|---|---|---|---|
| fact | 0.95 / 0.742 (21) | 0.81 / 0.755 (21) | 0.90 / 0.864 (21) |
| conditional | 0.57 / 0.510 (7) | 0.71 / 0.473 (7) | 0.86 / 0.576 (7) |
| cross_document | 0.62 / 0.498 (8) | 0.50 / 0.402 (8) | 0.75 / 0.499 (8) |
| table | 0.80 / 0.673 (5) | 0.80 / 0.700 (5) | 0.80 / 0.804 (5) |
| version | 0.75 / 0.531 (4) | 0.75 / 0.653 (4) | 1.00 / 0.800 (4) |

Missed pairs. Vector (9): q004 4.4, q012 6.2, q033 5.2, q044 12.1, q045 1.5.1 and 6.1, q048 11.2, q049 5.4.1, q050 10. Keyword (12): q004, q012, q022 3.4.1, q030 4.2.5, q033, q036 5.4.4, q042 5.4.2, q045 6.1, q047 11.2, q048, q049, q050. Hybrid (6): q004, q012, q022, q045 6.1, q048, q049. Hybrid rescued q033, q044, q045 (1.5.1) and q050, and lost q022.

### Score distributions (for the refusal rule in Step 4)

Top-1 score per question, min / median / max. Scores are not comparable across rankers. Separation is the chance that an answerable question's top-1 score is above an unanswerable one's (1.0 means no overlap).

| Ranker | Answerable top-1 | Unanswerable top-1 | Separation |
|---|---|---|---|
| vector (cosine similarity) | 0.820 / 0.860 / 0.909 | 0.811 / 0.833 / 0.840 | 0.915 |
| keyword (`ts_rank_cd`) | 0.5 / 1.4 / 3.1 | 0.3 / 0.9 / 1.3 | 0.757 |
| hybrid (RRF) | 0.030 / 0.033 / 0.033 | 0.031 / 0.032 / 0.033 | 0.772 |

The vector ranges overlap between 0.82 and 0.84, so a threshold on this score would refuse some answerable questions. The keyword score depends on the number of query words. The scores of the hit chunks are in the JSON report (`EVAL_REPORT_PATH`).

### Checks on the harness

- **Reproduction.** Vector R@5 0.800 and MRR 0.636, against 0.778 and 0.633 in the scratch evaluation (one pair apart). Keyword R@5 matches (0.733). Keyword MRR differs (0.633 against 0.598), as expected for a different ranking function.
- **Hybrid differs from the scratch figure** (0.867 against 0.76). The scratch script is gone, so the cause is not known. With 45 pairs, one pair is 0.022. Treat hybrid's lead over vector as a hypothesis for Step 2, not a finding.
- **Determinism.** Two runs with a random tie-break (the version ID) gave different keyword and hybrid numbers (keyword R@5 0.733 and 0.756; hybrid 0.889 and 0.867), because `ts_rank_cd` has many ties. After the md5 tie-break, two runs were identical apart from latency. Keyword order among ties is arbitrary unless it is fixed (Step 2).
- **Chunk counts.** The harness loads 180 chunks (157 current, 23 in retired v1.0). The scratch S1 set had 190. The production chunker gives 180 offline too. A size budget of about 1,030 characters would give 190, which fits a script that counted the title prefix against the 1,200. The scratch script is no longer available, so this is consistent with the evidence, not proven.
- **Planted failures.** Dropping the tenant context made the smoke test fail (the current version's chunks were invisible). Running the same questions as the admin role, which bypasses row-level security, is flagged by the retired-chunk counter (the CI negative control).

### Limits

- One owner-written question set and 45 pairs. Differences of one or two pairs are noise.
- 157 chunks and one tenant. The HNSW index and RLS filtering at scale are not tested. pgvector's HNSW filters after the index scan, so a filtered query can return fewer than k rows at scale. pgvector 0.8's iterative scans (`hnsw.iterative_scan`) are to be evaluated in Step 2 with a larger, multi-tenant dataset. This harness asserts k rows for the vector and hybrid rankers and reports any shortfall for all three.
- Latency is for a small table and an idle database.

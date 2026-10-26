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

## Retrieval strategy, Session 5 Step 2 (2026-10-26)

The evidence for ADR 0030. The first two parts come from `uv run pytest -m eval -s -k demo_set` (the harness, now with a k sweep). The last two come from `uv run pytest -m eval -s -k scale` (about 20 minutes). Both are outside CI and reproducible (RUNBOOK).

### Recall and MRR at k (45 pairs; recall@k / MRR@k, where MRR@k counts a first hit below rank k as 0)

| Ranker | k=3 | k=5 | k=8 | k=10 |
|---|---|---|---|---|
| vector | 0.756 / 0.611 | 0.800 / 0.621 | 0.867 / 0.629 | 0.889 / 0.632 |
| keyword | 0.733 / 0.611 | 0.733 / 0.611 | 0.800 / 0.622 | 0.844 / 0.627 |
| hybrid (constant 60) | 0.800 / 0.722 | 0.867 / 0.736 | 0.867 / 0.736 | 0.889 / 0.738 |

- At k=10 vector search reaches the same recall as hybrid (0.889). Hybrid's advantage is rank quality (MRR@5 0.736 against 0.621) and small k.
- The cost of more context is linear: chunks average 695 characters, so k=5 is about 3,500 characters, k=8 about 5,600, k=10 about 6,900 (about 870, 1,400 and 1,700 tokens at 4 characters per token, an estimate).
- Hybrid recall by question type at k = 3, 5, 8, 10: fact 0.90, 0.90, 0.90, 0.95; conditional 0.71, 0.86, 0.86, 0.86; cross_document 0.62, 0.75, 0.75, 0.75; table 0.80 at every k; version 0.75, 1.00, 1.00, 1.00. Cross-document questions do not gain from more chunks.

**Hybrid misses (pairs outside the top k):**

| Pair | Rank in hybrid | At k=8 | At k=10 | What it is |
|---|---|---|---|---|
| q022 3.4.1 | 10 | missed | **found** | The expected chunk is vector rank 1. Fusion with keyword search (which does not match "how fast" to "speed limits") moves it down. |
| q004 4.4 | 34 | missed | missed | A real semantic miss. The late-arrival clause sits in a chunk that mixes 4.1 to 4.5, and the vector rank is 53. |
| q012 6.2 | beyond the fetched depth | missed | missed | A labelling artefact: the top vector chunk is the right gifts table, but a table chunk carries only the section label (`6`), not `6.2`. |
| q045 6.1 | never | missed | missed | Always a miss by the retired-version rule. |
| q048 11.2 | 16 | missed | missed | A real miss (cross-document). |
| q049 5.4.1 | 18 | missed | missed | A real miss (cross-document). |

### Fusion constant

| Constant | k=3 | k=5 | k=8 | k=10 |
|---|---|---|---|---|
| 10 | 0.778 / 0.715 | 0.889 / 0.739 | 0.889 / 0.739 | 0.889 / 0.739 |
| 30 | 0.800 / 0.722 | 0.867 / 0.736 | 0.867 / 0.736 | 0.889 / 0.738 |
| **60** | 0.800 / 0.722 | 0.867 / 0.736 | 0.867 / 0.736 | 0.889 / 0.738 |
| 100 | 0.800 / 0.722 | 0.867 / 0.736 | 0.867 / 0.736 | 0.889 / 0.738 |

A constant of 10 gains one pair (q022 moves to rank 4). One pair is 0.022 on 45 pairs, which is noise, and choosing a constant on this set would overfit it. The standard 60 stays.

### The planner and the vector index, as one tenant grows

20 tenants: 19 ordinary ones (about 8,000 chunks, half of them retired versions, noisy copies of the demo chunks at a cosine of about 0.96 to their source) and one tenant that grows. The queries are the 50 questions, asked as `app_user` for the growing tenant. "Overlap" is the share of the exact top 5 that the plan returned. Fewest rows is the smallest number of rows returned for a query that asked for 50.

| Tenant chunks | Index | Plan the planner chose | p50 / p95 | Fewest rows | Queries with fewer than 5 | Overlap at 5 with exact |
|---|---|---|---|---|---|---|
| 2,006 | HNSW present | HNSW index scan | 6.1 / 12.0 ms | 2 | 11 | 0.292 |
| 5,007 | HNSW present | HNSW index scan | 8.1 / 15.1 ms | 4 | 1 | 0.336 |
| 10,013 | HNSW present | HNSW index scan | 5.7 / 11.0 ms | 7 | 0 | 0.324 |
| 20,018 | HNSW present | HNSW index scan | 7.3 / 12.9 ms | 15 | 0 | 0.316 |
| 2,006 | none | primary key (tenant first), exact sort | 20.3 / 41.0 ms | 50 | 0 | 1.000 |
| 5,007 | none | primary key, exact sort | 44.7 / 78.9 ms | 50 | 0 | 1.000 |
| 10,013 | none | primary key, exact sort | 113.6 / 192.7 ms | 50 | 0 | 1.000 |
| 20,018 | none | primary key, exact sort | 187.7 / 368.9 ms | 50 | 0 | 1.000 |

- With the tenant's 157 chunks (the demo set) among 8,388, the planner used the tenant-first primary key and the answer was exact, at about 8 ms p50. That was measured in an earlier run of this experiment, not by the committed test.
- With the index present, the planner chooses it from about 2,000 chunks, and the result is fast and silently wrong: about a third of the exact top 5 is returned, and some queries come back with far fewer rows than asked for.
- Without the index the result is exact at every size, and the cost grows with the tenant's own size. p95 passes 250 ms between 10,000 and 20,000 chunks.
- A scratch run before this test gave overlaps of 0.74 to 0.84 for the same experiment, with the same plans and the same shortfall. The cause of the difference is unexplained. The committed test is the reference, and the decision does not depend on which is right.

### pgvector's settings, when the index is the only way to narrow

Run with the primary key removed so that HNSW is used (pgvector 0.8.7, `hnsw.ef_search`, `hnsw.iterative_scan`). The tenant under test has the real demo set, 157 visible chunks of 8,388. "Pairs" is the number of the 45 expected pairs with a hit in the top 5 (exact search: 36).

Moderate overlap (copies at a cosine of about 0.91 to their source):

| Setting | Fewest rows | Overlap at 5 | Pairs | p50 / p95 |
|---|---|---|---|---|
| default (ef_search 40) | 17 | 0.812 | 30 | 4.9 / 7.2 ms |
| ef_search 100 | 39 | 0.912 | 32 | 7.0 / 11.7 ms |
| ef_search 200 | 50 | 0.968 | 36 | 8.4 / 20.7 ms |
| ef_search 400 | 50 | 0.988 | 36 | 10.4 / 17.9 ms |
| strict iterative, ef 40 | 50 | 0.812 | 30 | 7.5 / 13.1 ms |
| strict iterative, ef 100 | 50 | 0.912 | 32 | 7.3 / 10.8 ms |
| relaxed iterative, ef 40 | 50 | 0.908 | 32 | 6.5 / 10.3 ms |
| relaxed iterative, ef 100 | 50 | 0.916 | 32 | 6.9 / 17.3 ms |

Near-duplicate content (copies at a cosine of about 0.99, which policy corpora have: versions, boilerplate):

| Setting | Fewest rows | Queries with fewer than 5 | Overlap at 5 | Pairs | p50 / p95 |
|---|---|---|---|---|---|
| default | 1 | **50** | 0.244 | 20 | 3.0 / 3.6 ms |
| ef_search 100 | 1 | 46 | 0.320 | 23 | 3.4 / 6.5 ms |
| ef_search 200 | 1 | 14 | 0.420 | 26 | 4.2 / 6.1 ms |
| ef_search 400 | 1 | 5 | 0.564 | 27 | 5.4 / 11.6 ms |
| strict iterative, ef 40 | 1 | 5 | 0.308 | 24 | 39.6 / 83.8 ms |
| strict iterative, ef 100 | 1 | 5 | 0.352 | 26 | 38.5 / 70.1 ms |
| relaxed iterative, ef 40 | 1 | 5 | 0.792 | 31 | 16.6 / 30.4 ms |
| relaxed iterative, ef 100 | 1 | 5 | 0.796 | 31 | 16.8 / 35.3 ms |

On moderate overlap a larger `ef_search` (200 or more) matches exact search at about 8 to 10 ms. On near-duplicate content no setting tried matches exact search (36 pairs): the best reaches 31, five queries still return fewer than 5 rows, and strict iterative scans cost 40 ms. The reason five queries stay short was not diagnosed.

### Query embedding in the web process (one process, measured once on the development machine)

| Measure | Value |
|---|---|
| Working set before the model loads | 51 MiB |
| Load (including a 2.8 s hash check of the 448 MiB file) | 9.2 s |
| Working set after load, steady | 752 to 762 MiB |
| Peak during load | 1,115 MiB |
| First query | 35 ms |
| One query alone | p50 28.6 ms, p95 41.8 ms |
| 50 queries run on the event loop | the loop is blocked for 1,569 ms |
| 50 queries in threads, 1 at a time | loop lag at most 17.5 ms; throughput 43 queries per second |
| 4 at a time | loop lag at most 49 ms; 62 queries per second; latency p50 62 ms |
| 8 at a time | 54 queries per second; latency p50 124 ms (no gain over 4) |

### Limits

- 45 pairs, so one pair is 0.022. The hybrid lead over vector search is a hypothesis: the old scratch figure of 0.76 is unexplained, and at k=10 vector search equals hybrid.
- The scale data is synthetic copies of the real chunks, with noise of my choosing. The embeddings of the real tenant are real.
- Timings are from one development machine with an idle database.
- Keyword search at 20,000 chunks per tenant was not measured.

## Answer providers, Session 5 Step 3b (2026-10-26)

What this is: whether each provider works, what it costs and how fast it is, with the retrieved top 5 chunks of each of the 50 demo questions as sources. It is not an answer-quality score; the answered/refused counts are a sanity check (quality is Steps 4 and 7). Fictional data only. Reproduce with `uv run pytest -m eval -s -k answer_providers` (RUNBOOK, "Answer providers"). Costs are list prices (the free tier charged nothing).

### Groq `openai/gpt-oss-120b`, free tier, reasoning effort `low`, 50 of 50 calls

| Measure | Value |
|---|---|
| Calls ok | 50 of 50, no errors (one earlier call failed, see below) |
| Tokens | input 72,701; output 5,617 (of which reasoning 2,480); 1,566 per answer, output 112 per answer |
| Reasoning per answer | 10 to 135 tokens (mean 50) |
| Latency | p50 760 ms, p95 1,139 ms, worst 4,483 ms (target p95 8,000 ms: met) |
| List-price cost per answer | mean $0.00029, p95 $0.00034 (target $0.002: met) |
| Sanity | answered 36 of 40 answerable; refused 10 of 10 unanswerable |
| Free limits on this account (response headers) | 1,000 requests per day, 8,000 tokens per minute |

- **Reasoning is a small share at effort `low`:** 44% of the output tokens, about 50 per answer. Input is 93% of all tokens, so the daily limit is spent mostly on the sources. The 200,000 tokens per day figure comes from Groq's documentation, not from headers. About 95,000 tokens were used on the day of these runs (three runs: 5, 6 and 50 questions), so about 105,000 were left by that estimate.
- **Pacing:** 8,000 tokens per minute means about 5 answers a minute at 1,570 tokens each. The 50-question run therefore takes about 13 minutes at the 12 s pace.
- **Strict schema is not a guarantee.** On one earlier run, the sixth call came back as HTTP 400 `json_validate_failed`: the model's own JSON broke the schema (a citation of the wrong type) although strict mode was on. The runner first treated that as a rejected request and stopped. It is now typed as invalid output (a test covers it), and the 50-call run had no such failure. One failure in about 61 calls is a rate to watch, not a measurement of it.
- **Four answerable questions were refused:** q004 and q022 (both hybrid misses at k=5, ADR 0030, so the right chunk was probably not among the sources) and q043 and q044 (version questions; why they were refused was not examined). This is not scored here. Step 7 judges it.

### Gemini `gemini-2.5-flash-lite`, free tier: not available

The first call returned HTTP 404: "This model models/gemini-2.5-flash-lite is no longer available to new users", pointing to `gemini-3.5-flash-lite`. Google's deprecation page confirms that access to the 2.5 models is limited to users who have used them before. So this account cannot test the model in ADR 0031's candidate list. Whether it supports a response schema was therefore not tested. No quota was spent (the runner stops on the first rejected call).

### Anthropic Claude Haiku 5.5: not live-tested

No paid account during development. The adapter is tested offline against fake HTTP responses only.

### Limits of this evidence

50 questions, one run per provider, one development machine and network, one day's free-tier conditions. Latency is the successful call, without rate-limit waits. The failure rate of strict schema output needs more calls than this.

### Gemini `gemini-3.5-flash-lite`, free tier: 5 of 5 calls (a smoke test, not a comparison)

One 5-question call, on a key from a project with no billing. Schema support is shown by every output parsing against the response schema, and by Google's model page ("Structured outputs: Supported").

| Measure | Value |
|---|---|
| Calls ok | 5 of 5, no errors |
| Tokens | input 6,930; output 222 (reasoning 0 reported); 1,430 per answer |
| Latency | p50 1,126 ms, p95 1,883 ms (target 8,000 ms: met) |
| List-price cost per answer | mean $0.00053, p95 $0.00060 (target $0.002: met; list price $0.30 / $2.50 per million tokens) |
| Sanity | answered 3 of 4 answerable; refused 1 of 1 unanswerable |
| Free limits | unknown: Google sends no rate-limit headers; the limits are shown only in AI Studio |

Five calls say it works and what each costs. They do not measure its daily capacity, its failure rate, or its quality against Groq.

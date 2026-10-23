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

# ADR 0030: Retrieval Strategy

**Status:** Accepted. Amends ADR 0021, decision 3 (the vector index and "whether retrieval uses vector search, keyword search, a fusion of the two, or a reranker is decided in Session 5").

**Date:** 2026-10-26

**Deciders:** Hamza Asif

## Context

Session 5 answers a question from the tenant's own documents. Before an answer can be written, the system must find the few chunks that may contain it. ADR 0021 stored both an embedding and a `tsvector` per chunk and left the retrieval strategy open. This ADR decides it, using the evaluation harness (Step 1) and a scale test (Step 2).

Constraints that shape the choice:
- Every retrieval query runs as `app_user`, so row-level security keeps one tenant's current chunks and nothing else (ARCHITECTURE 5.6).
- SPEC 7.2 asks for recall@5 of at least 0.85. SPEC 6.2 asks for hybrid retrieval (keyword and vector, merged), with reranking optional.
- The answer step is paid per token, and a zero-cost constraint applies (SPEC 7.5), so the number of chunks sent matters.
- Policy corpora repeat themselves: versions of one document, and boilerplate shared by documents and tenants.

## Decisions

### 1. Strategy: hybrid search with reciprocal rank fusion, constant 60

- **Vector:** cosine similarity between the question's embedding (with the `query:` prefix) and the chunk embeddings, exact (decision 2).
- **Keyword:** the question's words stemmed by the English configuration, joined with OR, matched against the stored `tsvector` and ranked with `ts_rank_cd`. OR, because a question has words that no single chunk contains. PostgreSQL's rank functions have no inverse document frequency, so this is not BM25.
- **Fusion:** reciprocal rank fusion, score = sum over the two lists of 1 / (60 + rank), over the top 50 of each list. A chunk missing from a list adds nothing from it, so a question with no keyword match (a stop-word-only question, or Roman Urdu or Urdu script, which the English configuration cannot match, ADR 0021) keeps the vector order.
- **Ties** are broken by the md5 of the chunk text, then `(version_id, position)`. `version_id` is a random UUID, and on its own it made two loads of the same files rank differently (Step 1).
- The constant stays at the standard 60. A constant of 10 gained one pair on 45, which is noise, and tuning it on this set would overfit it.

### 2. No vector index: exact search over the tenant's rows (migration `0016_drop_hnsw_index`)

pgvector's HNSW index is approximate and filters after its scan. Row-level security is such a filter, so a tenant's query asks the index for the nearest rows of all tenants and then loses the rows it may not see. The planner chooses the index once a tenant has about 2,000 chunks, and from then on the answer is wrong without an error (evidence below). Tuning did not make it safe on content that other tenants repeat, which policy corpora do.

Without the index, the planner narrows by the tenant-first primary key and sorts that tenant's rows by exact distance. The result is exact at every size tested. The cost grows with the tenant's own chunk count, not with the table's.

- This is a new migration, because 0008 is applied. The downgrade recreates the index as 0008 made it.
- The keyword index (`chunks_tsv_gin`) stays.
- **Revisit** (partitioning the chunks by tenant, or an index per tenant, so that no index holds rows to filter out) when retrieval p95 is above about 250 ms in the deployed system, or when one tenant has more than about 10,000 chunks. A plain HNSW index must not be added back.

### 3. How many chunks: a setting, `RETRIEVAL_TOP_K`, default 5

Range 1 to 20, read by the web app. Recall is the same at 5 and 8 (0.867), and one pair more at 10 (0.889). Cross-document answers may need more context than recall shows, so the answer evaluation (Step 7) compares 5 and 8 and the default may change then.

### 4. No reranker

Hybrid search meets the SPEC target at k=5 and k=8 (39 of 45 pairs, 0.867, by one pair). The remaining misses at k=10 are one retired-version pair that cannot be found, one labelling artefact (q012, below), and three real misses far down the list (ranks 16 to 34). A reranker could help the real ones, but it adds memory on top of an embedder that already needs about 1.1 GB per process, and nothing here shows that it is needed. Revisit if the answer evaluation (Step 7) shows missing evidence.

### 5. Query embedding in the web process

- The model loads once, in the application's lifespan, before the application is ready. `/ready` fails until it is loaded.
- Each question is embedded with `asyncio.to_thread`, bounded to about 4 at a time.
- One worker per container. Each process holds about 0.76 GB steady and 1.1 GB at its peak while loading.
- This is built with `POST /ask` (Step 5), not in this step.

### 6. The `retrieve()` contract

`retrieve(session, question, query_vector, top_k=...)` takes a session from `begin_tenant_transaction`, adds no tenant or version filter of its own, and does not open a transaction. It returns exactly `top_k` chunks, best first, or fewer when the tenant has fewer visible chunks. Each carries the text, the document title, the version number, the section, the heading, the clause labels, the fused score and the rank in each list. It refuses an empty or over-long question, a vector that is not 384 finite numbers, and a count outside 1 to the depth, before it touches the database. It logs counts only.

## Evidence

Full tables are in `docs/EVAL.md` ("Retrieval strategy, Session 5 Step 2"), and are reproduced by `pytest -m eval` (the harness) and `pytest -m eval -k scale`.

**Recall and MRR at k** (45 pairs; recall@k / MRR@k):

| Ranker | k=3 | k=5 | k=8 | k=10 |
|---|---|---|---|---|
| vector | 0.756 / 0.611 | 0.800 / 0.621 | 0.867 / 0.629 | 0.889 / 0.632 |
| keyword | 0.733 / 0.611 | 0.733 / 0.611 | 0.800 / 0.622 | 0.844 / 0.627 |
| hybrid | 0.800 / 0.722 | 0.867 / 0.736 | 0.867 / 0.736 | 0.889 / 0.738 |

**The six hybrid misses at k=5:** q022 3.4.1 (rank 10, found at k=10), q004 4.4 (rank 34), q012 6.2 (beyond the fetched depth; see below), q045 6.1 (never; retired by rule), q048 11.2 (rank 16), q049 5.4.1 (rank 18). At k=8 all six remain; at k=10 five remain.

**The planner, as one tenant grows** (20 tenants, up to about 28,000 chunks; the answer is the share of the exact top 5 returned):

| Tenant chunks | With the HNSW index | Without it |
|---|---|---|
| 2,006 | HNSW chosen; overlap 0.29; fewest rows 2 of 50 | exact; p50 20 ms, p95 41 ms |
| 5,007 | HNSW chosen; overlap 0.34; fewest rows 4 | exact; p50 45 ms, p95 79 ms |
| 10,013 | HNSW chosen; overlap 0.32; fewest rows 7 | exact; p50 114 ms, p95 193 ms |
| 20,018 | HNSW chosen; overlap 0.32; fewest rows 15 | exact; p50 188 ms, p95 369 ms |

**Tuning pgvector** (the index forced, other tenants' content nearly identical): with default settings every one of 50 queries returned fewer than 5 rows, and 20 of the 45 pairs were found, against 36 for exact search. Larger `ef_search`, strict iterative scans and relaxed iterative scans each helped, none reached 36 (the best reached 31), and five queries still returned fewer than 5 rows. On moderately overlapping content, `ef_search` 200 or more matched exact search.

## Caveats (carried from Step 1, and new)

- **45 pairs.** One pair is 0.022. Differences of one or two pairs are noise.
- **The hybrid lead is a hypothesis.** The scratch evaluation of Session 4 gave hybrid 0.76, below vector (0.78); this harness gives 0.867 against 0.800. The cause of the difference is unexplained. At k=10 vector search equals hybrid (0.889). Hybrid is chosen for its better rank quality (MRR@5 0.736 against 0.621), because SPEC 6.2 asks for it, and because it costs only about 8 ms more (p50 15.8 ms against 7.8 ms), not because 45 pairs prove it.
- **Tie-break.** Until Step 1 fixed it, keyword and hybrid results depended on random ids, and hybrid moved between 0.867 and 0.889 on that alone.
- **180 vs 190 chunks** against the scratch set is unexplained; a size budget of about 1,030 characters would give 190.
- **q012 is a labelling artefact.** A table chunk carries only the section label, so a citation of a table is section-level until ingestion changes (an open decision in PROGRESS).
- **The scale data is synthetic**: noisy copies of the real chunks, with the noise chosen by me. A scratch run of the growth experiment gave overlaps of 0.74 to 0.84 against 0.29 to 0.34 in the committed test, with the same plans and the same shortfall; the cause is unexplained. The committed test is the reference.
- **Timings** are from one development machine with an idle database. Keyword search at 20,000 chunks per tenant was not measured.
- **The embedder**'s load time and memory are for this machine (Windows, onnxruntime, 2 threads).

## Rejected options

- **Keep the HNSW index with defaults.** It returned fewer rows or the wrong nearest ones from 2,000 chunks, silently.
- **Keep it and tune it** (`ef_search`, iterative scans). Correctness would depend on the planner and on how similar other tenants' documents are, and both change without notice. Tuning failed on near-duplicate content.
- **Partition by tenant or index per tenant now.** Sound, and the way forward at scale, but it adds moving parts that a tenant of a few hundred chunks does not need. It is the revisit step.
- **Vector search only.** Recall matches hybrid at k=10, but MRR is lower and the SPEC asks for hybrid. A single list is also what hybrid falls back to when a question has no keyword match.
- **A reranker.** Not needed to meet the target (decision 4).
- **A different fusion constant.** No clear winner on 45 pairs (decision 1).

## Consequences

- **Positive:** retrieval is exact and tenant-scoped at every tested size, with no planner-dependent behaviour. One copy of the ranking SQL serves the app and the evaluation.
- **Negative:** the vector query costs more as a tenant grows (188 ms p50 at 20,000 chunks), so there is a revisit threshold to watch. The first tenants are far below it (the demo set has 157 chunks).
- **Negative:** keyword search is English only; Roman Urdu and Urdu questions rely on the vector side alone (ADR 0021).
- **Operational:** the dev database needs `alembic upgrade head` (migration 0016). Until then `/ready` returns 503 `schema`.

## Related decisions

- ADR 0021 (embedding model, chunking, the stored `tsvector`; its decision 3 is amended here)
- ADR 0022 (ingestion, which writes the chunks)
- ARCHITECTURE 5.6 (the retrieval rule that row-level security enforces)
- ADR 0031 (answer model, next)

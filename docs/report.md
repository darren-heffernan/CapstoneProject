# Knowledge Box — Project Report

**A self-hosted retrieval-augmented generation (RAG) microservice for suggesting remedial actions to maintenance call-outs.**

Author: Darren Heffernan
Repository: https://github.com/darren-heffernan/CapstoneProject/

---

## 1. Abstract

Knowledge Box is a retrieval-augmented generation (RAG) microservice that suggests a concise remedial action to maintenance call-outs, pulling from corpus of roughly 59,000 historical cases. It takes a free-text description of a fault, embeds it with a sentence-transformer model, retrieves the most similar historical faults from a Postgres/pgvector index, and passes those retrieved cases as grounding context to a locally served large language model (LLM) via Ollama. A suggested fix is then presented along with the supporting historical cases.

The entire pipeline is self-hosted: no fault data or query ever leaves the local environment and no external LLM API is called. This was a hard requirement due to the commercially sensitive nature of the maintenance data. The complete pipeline has been tested end to end against the real anonymised workbook (80,117 raw rows reduced to 59,063 indexed records after cleaning, category filtering and deduplication).

---

## 2. Problem statement and motivation

On a production line, when a unit fails a test or a machine faults, an engineer is called out to diagnose and fix it. The fix is logged: a short free-text fault description (e.g. "Sound fail on final test, speaker output silent") and the remedial action taken (e.g. "Replaced faulty speaker module, RTV to supplier"). Over years this accumulates into a large maintenance workbook (tens of thousands of fault/fix pairs).

That history is valuable but effectively inaccessible. It lives in a spreadsheet, and the only way to benefit from it is if the engineer on shift happens to remember that a near-identical fault was resolved a certain way months ago. New or less experienced engineers cannot draw on it at all. The same faults get re-diagnosed from scratch.

The goal of Knowledge Box is to make that accumulated history queryable in natural language. An engineer describes the fault in their own words and is presented with the historical call-outs most similar to it and a synthesised suggested action.

### Why RAG rather than a fine-tuned or plain LLM

- RAG keeps the data in a queryable index, cites the exact rows a suggestion is grounded in, and updates simply by re-running ingest.
- A plain LLM has no knowledge of Ei Electronics' machines' faults, part numbers, test stations or conventions, and would hallucinate plausible-but-wrong fixes.
- Fine-tuning a model on the workbook would bake the data into weights, is expensive to repeat as data grows, and still gives ungrounded, untraceable output.

---

## 3. Background and related work

Knowledge Box uses principles from three AI topics: retrieval-augmented generation, dense semantic retrieval and (classically) case-based reasoning.

### 3.1 Retrieval-augmented generation (RAG)

RAG models *combine pre-trained parametric and non-parametric memory for language generation* [1]. Combining a model's parametric memory (weights) with non-parametric memory (an external, retrievable corpus) results in *more specific, diverse and factual language than a state-of-the-art parametric-only seq2seq baseline*[1]. The central benefit for this project is that grounding a generator in retrieved evidence reduces hallucination [7].

### 3.2 Dense retrieval and sentence embeddings

Karpukhin et al. [2] showed that dense passage retrieval outperforms the then-de facto sparse vector space models, such as TF-IDF or BM25 for open-domain question answering. Sentence-BERT [3] made this practical by producing sentence-level embeddings that can be compared directly with cosine similarity. Knowledge Box uses `all-MiniLM-L6-v2`, a compact sentence-transformers model built on Microsoft's MiniLM distillation work [4].

### 3.3 Approximate nearest-neighbour search and vector stores

At ~59k rows an exact scan is tolerable, but the corpus grows, so the index uses approximate nearest-neighbour (ANN) search. The specific structure is HNSW (Hierarchical Navigable Small World) graphs [5].
Alternatives considered were the FAISS library and dedicated vector databases (e.g. Pinecone). The choice of pgvector keeps the embeddings alongside their relational metadata (bay, cell, category, remedial action) in a single Postgres instance that is easily self-hostable.

### 3.4 Case-based reasoning: the classical lineage

Knowledge Box is fundamentally a case-based reasoning (CBR) system. Aamodt & Plaza's [6] frame CBR as a four-step cycle:
  - **Retrieve** a similar past case
  - **Reuse** its solution
  - **Revise** it for the new situation
  - **Retain** the outcome

Knowledge Box implements the first two steps directly: pgvector similarity search is *retrieve*, and the LLM's synthesis of a suggested action from the retrieved fault/fix pairs is *reuse*. *Revise* and *retain* are deliberately left as future work (§13).

### 3.5 Existing solutions

Several mature options could implement parts of this system. Here are some that were considered:

| Option | What it offers | Why not adopted here |
|---|---|---|
| Hosted LLM QA (OpenAI, Anthropic, etc.) | High-quality generation with no infrastructure | Ruled out by C1 - the corpus and queries cannot leave the environment |
| Managed vector DB (Pinecone) | Turnkey, scalable similarity search | Ruled out by C1 - data would leave the host; also a second external dependency |
| RAG frameworks (LlamaIndex) | Pre-built retrieval & generation wiring | Self-hostable, but heavy abstraction and dependency weight for a pipeline that is only a few hundred lines. Favoured a minimal implementation. |
| Self-hosted vector DB (Qdrant) | Purpose-built vector store you run | Adds a second data system beside Postgres; pgvector keeps vectors and metadata in one store |
| CMMS / enterprise keyword search | Existing maintenance-log search | Expensive license, usually part of a larger suite.|

---

## 4. Constraints

| # | Constraint | Rationale / consequence |
|---|---|---|
| C1 | **Fully self-hosted. No external LLM APIs anywhere in the pipeline.** | The maintenance data is commercially sensitive; it cannot be sent to a hosted API. |
| C2 | **Git tracks code and config only, never data or secrets.** | The real workbook and `.env` must not enter version control. The DB and model artifacts rebuild locally from `ingest.py`, so any machine can be reconstructed from git and the raw workbook alone. |
| C3 | **All embedding calls go through one wrapper function.** | A fine-tuning swap is anticipated. Routing every embed call through `app/embeddings.py` means the model can change in one place without touching index-time or query-time call sites, and keeps the two consistent by construction. |
| C4 | **Non-fault categories are filtered at index time.** | Categories like Changeover, Operator Error, No fault found, Preventative Maintenance and Call out cancelled do not represent a fault→fix pattern worth retrieving, and would pollute results. |
| C5 | **Runs across two machines** - a laptop for code, a Docker/GPU host for the runtime. | See §8. Reinforces C2: the split only works because nothing but code/config is synced. |

---

## 5. System architecture

Knowledge Box is a small FastAPI service in front of two stateful backing services (a pgvector-enabled Postgres and an Ollama model server), fed by an offline ingest pipeline.

![Knowledge Box system architecture: an offline ingest pipeline (raw workbook to cleaning to filtering/dedup to embedding to Postgres/pgvector) and an online serve path (browser form to FastAPI /suggest to embedding to pgvector search to Ollama and back), both calling the same embedding wrapper, all within a self-hosted boundary.](architecture.svg)


Both the offline ingest path and the online query path call the *same* embedding wrapper, so index-time and query-time text are embedded identically.

</details>

**Key point:** the embedding wrapper (`app/embeddings.py`) is shared by both the offline ingest path and the online query path (dashed link above).

### Component summary

| Component | File | Responsibility |
|---|---|---|
| Ingest pipeline | `scripts/ingest.py` | Load workbook → clean → filter → embed → upsert into Postgres |
| Embedding wrapper | `app/embeddings.py` | Single choke point turning text into normalised vectors |
| API service | `app/main.py` | `POST /suggest`: embed query → retrieve → prompt LLM → return |
| Frontend | `app/static/index.html` | Plain HTML/JS form, served by FastAPI, no build step |
| Backing services | `docker-compose.yml` | pgvector Postgres + Ollama (and, in the `full` profile, the API) |

---

## 6. Data pipeline (ingest)

`scripts/ingest.py` is an idempotent batch job: raw workbook in, embedded Postgres index out. It runs directly (`python scripts/ingest.py`) and can be re-run safely at any time.

### 6.1 Loading and column normalisation

Two mechanisms handle ingesting the real-world spreadsheet data messiness:

1. **Generic punctuation stripping.** `_clean_column_name` collapses any run of non-alphanumeric characters to a single underscore and lower-cases the result e.g.`Bay #` → `bay`, `Time to resolve (mins)` → `time_to_resolve_mins`.
2. **Explicit aliases for semantic renames** `COLUMN_ALIASES` maps `product` → `product_family` and the fault-description column → `fault_description`.

The design intent is that a future export with different wording should be accommodated by extending the alias dict, not changing the schema. Required columns (`fault_description`, `remedial_action`, `category`) that are still missing after normalisation raise a clear error listing the columns that *were* found.

### 6.2 Cleaning and filtering

`_clean` then:

- strips whitespace and drops rows with an empty `fault_description` or `remedial_action`;
- filters out non-fault categories (constraint C4): Changeover, Operator Error, No fault found, Preventative Maintenance, Call out cancelled;
- coerces `date` and `time_to_resolve_mins` to proper types, tolerating bad values via `errors="coerce"`.

Every step logs how many rows it dropped.

### 6.3 Deduplication

The table's primary key is a `row_hash`. A SHA-256 over date/shift/bay/cell/fault_description/remedial_action. Combined with the `ON CONFLICT (row_hash) DO UPDATE` upsert. This makes ingest idempotent and collapses exact-duplicate call-out entries in the source. On the real workbook this collapsed 253 rows; spot-checking confirmed these were genuine duplicate entries, not distinct incidents being lost.

### 6.4 Embedding and storage

Fault descriptions are embedded in a batch via `embed_texts`, and rows + vectors are upserted into `maintenance_records`. The schema is created if absent, including the pgvector extension and a HNSW index [5] with `vector_cosine_ops` for approximate-nearest-neighbour search. Embeddings are L2-normalised, so cosine distance and inner product agree.

---

## 7. Retrieval and generation (the `/suggest` path)

`app/main.py` exposes `POST /suggest`. Given a `SuggestRequest` (`fault_description`, optional `product_family` / `test_station`, and `top_k` bounded to 1–20), the handler:

1. Embeds the query via `embed_text` (the same wrapper used at index time (C3)).
2. Retrieves the `top_k` most similar rows with a pgvector similarity query, ordering by `embedding <=> %s::vector` (cosine distance). Note: without the `::vector` cast, Postgres cannot infer the parameter type and raises `operator does not exist: vector <=> double precision[]`.
3. Builds a grounding prompt (`_build_prompt`) that lays out the retrieved fault/fix pairs as numbered context, appends any optional product-family/test-station context, and asks the model for a concise remedial action grounded in those resolutions.
4. Generates the suggestion by calling the local Ollama `/api/generate` endpoint (`_generate_suggestion`). If Ollama is unreachable it returns a clean `502` naming the host, rather than a stack trace.
5. Returns the `suggested_action` together with the full list of `supporting_cases`, including each case's cosine distance, which the frontend renders as a "% similarity" score.

Returning the supporting cases is a core design choice: the suggestion is traceable. An engineer can see the historical rows it was built from and decide for themselves whether to trust it.

### Reliability details

- **Startup warm-up.** A FastAPI `lifespan` handler warms both the embedding model and the Ollama model at startup, so the first real request is not penalised by cold-loading a multi-GB model.
- **Generous timeout.** Ollama calls use a 300s timeout. On CPU-only hardware the first generation is genuinely slow, and a client giving up early would otherwise abort the model load entirely.
- **Empty-index guard.** If no records are indexed yet, `/suggest` returns a `404` telling the caller to run ingest first, rather than failing obscurely.
- **Optional weak-match guard.** If `KBOX_MAX_DISTANCE` is set and the nearest retrieved case is farther than it, the response is flagged `low_confidence` with an explanatory note (the suggestion is still returned). Disabled by default; see §9.2 for why this guards the "no similar history" case rather than acting as a relevance filter.

---

## 8. Deployment and the two-machine workflow

Because git carries only code and config (C2), the system runs cleanly across two machines:

- **Machine A (laptop)** write code, commit, push. No data, no `.env`, no containers needed.
- **Machine B (Docker/GPU host)** real workbook under `data/raw/`, `docker compose up -d`, then `python scripts/ingest.py` to rebuild Postgres and embeddings locally.


For a shared-server deployment (so colleagues on the office network can reach the API), `docker-compose.yml` defines a `full` profile that additionally containerises the FastAPI app (`Dockerfile`). Local dev keeps running uvicorn in a venv against just the DB and Ollama; the server runs everything in containers. The `app` service overrides `POSTGRES_HOST`/`OLLAMA_HOST` to the container network names, so a single `.env` works in both cases. See `docs/deployment.md` for the full walkthrough.

**Security posture (deliberately honest).** In the shared-server setup, the DB (5432) and Ollama (11434) ports are bound to `127.0.0.1` only. Postgres ships with the default `kbox`/`kbox` credentials. Only the app's port 8000 is published network-wide. However, `POST /suggest` currently has no authentication: anyone who can reach the host on port 8000 can query it and, by extension, the maintenance history it is grounded in. This is called out in `docs/deployment.md` as something to resolve (an API-key check or similar) before any exposure beyond a trusted network, and is listed again under Limitations below.

---

## 9. Evaluation

`scripts/evaluate.py` (documented in `docs/evaluation.md`) reports precision@k / recall@k / success@k / MRR over the same retriever `/suggest` uses. It has been run against the real indexed corpus; §9.2 gives the numbers, alongside the end-to-end and design-level checks below.

### 9.1 End-to-end run on the real corpus

The full pipeline was run against the real anonymised workbook:

| Stage | Rows |
|---|---|
| Raw rows loaded | 80,117 |
| After cleaning (blank fault/fix dropped) + non-fault category filter | ~59k |
| After row-hash deduplication (253 exact duplicates collapsed) | 59,063 indexed |

The pipeline completes end to end, the index builds, and `/suggest` returns grounded suggestions with supporting cases.

### 9.2 Retrieval quality

The harness was run against the real indexed corpus in two modes, both scored over the exact retriever `/suggest` uses.

**Auto / leave-one-out mode** (200 sampled rows; each row's own fault text is the query, and other rows sharing its category count as relevant):

| k | Precision@k | Success@k |
|---|---|---|
| 1 | 0.345 | 0.345 |
| 3 | 0.305 | 0.550 |
| 5 | 0.301 | 0.675 |
| 10 | 0.288 | 0.810 |

MRR 0.484. By the top 10 results, 81% of queries surface at least one same-category historical record, with the first such hit at rank ~2 on average.

**Labelled mode** (9 hand-authored natural-language queries; relevance = a matching category plus an on-theme keyword: see `docs/eval/labelled_queries.json`):

| k | Precision@k | Success@k |
|---|---|---|
| 1 | 0.111 | 0.111 |
| 3 | 0.111 | 0.222 |
| 5 | 0.222 | 0.556 |
| 10 | 0.178 | 0.667 |

MRR 0.261.

The lower labelled-mode scores (MRR 0.26 vs 0.48; success@10 0.67 vs 0.81) put a number on the style-sensitivity of a general-purpose embedding model on domain shorthand:

- **Strong when the query is phrased like the historical logs.** Terse component-code shorthand retrieves near-identically e.g. `"Failed on CR51"` matched a historical entry at cosine distance ≈ 0.
- **Weaker for natural-language paraphrases of code-heavy categories.** A plain-English rephrasing of a terse, code-laden category such as `Contact problem` retrieves less reliably.

Recall@k is reported, but is not meaningful in either mode. precision@k, success@k and MRR are the informative measures here. The labelled set is small and its keyword-based relevance is an engineer-checkable approximation.

Dumping the cosine distances of retrieved rows (`--dump-distances`) shows the relevant and non-relevant distributions overlapping heavily (both have a median of 0.000, diverging only in the upper tail (relevant p90 ≈ 0.29 vs non-relevant p90 ≈ 0.38)). Identical strings (e.g. "Frozen", "down") recur across different categories, so many non-relevant rows sit at distance ≈ 0. Two implications: (a) a distance threshold cannot be used to improve retrieval precision and (b) the only defensible use of a threshold is to detect the "no similar history at all" case. The optional, off-by-default `KBOX_MAX_DISTANCE` guard (§7) flags a response `low_confidence` rather than filtering, since filtering by distance would not help.

### 9.3 Design-level validation

- **Idempotency** of ingest was confirmed: re-running upserts on `row_hash` without needing a fresh database. Rows deleted from the source workbook are not removed from the table.
- **Deduplication correctness** was spot-checked: the 253 collapsed rows were all true duplicates.
- **Category filtering** is demonstrated even on `data/sample.csv`, which deliberately includes a few rows in each filtered-out category.

### 9.4 Unit tests

The pure-Python cleaning and normalisation logic is covered by a `pytest` suite in `tests/` that runs with no Docker, database or model required. It exercises header normalisation, the alias map, missing-required-column errors, blank-row dropping, non-fault category filtering, and the row-hash. See §11.

### 9.5 Gaps

A larger, engineer-labelled set with tighter per-query relevant sets is the priority next step (§13).

To close the gap left by noisy category labels, a small human relevance study is designed and scheduled. An engineer rates whether each of the top-k retrieved cases is a useful match for a sample of real faults, yielding a human usefulness rate and success@k. The protocol and a ready-to-run form generator (`scripts/make_human_eval.py`, producing a self-contained rating page) are in `docs/eval/human_eval_protocol.md`. This study is prepared but has not yet been run; its results are not included in this report.

---

## 10. Key design decisions and trade-offs


1. **Self-hosted everything (C1).** Trade-off: local Ollama on CPU is slow (tens of seconds per generation) and the operator must manage model pulls and a GPU host for good performance. Accepted because sending fault data to a hosted API was never an option.
2. **Single embedding wrapper (C3).** A small indirection that buys guaranteed index/query consistency and a one-line model swap later.
3. **Filter non-fault categories at index time, but do not over-filter (C4).** The real taxonomy is far more granular than the five filtered categories and includes mixed-content categories (`Cell set-up`, `New line set-up`) that contain real troubleshooting narratives alongside setup logistics. Decision: do not expand the filter list, because filtering a whole category would discard genuine fault/fix content along with the noise. Revisit only if retrieval quality demonstrably suffers.
4. **Frontend served by FastAPI, not a second framework.** The demo UI is a single static HTML/JS page served via `StaticFiles`. Standing up a separate Flask service would mean another process, port and container for no benefit; FastAPI serves HTML fine. Revisit only if the frontend must be deployed independently of the API.
5. **HNSW index with cosine ops + normalised embeddings [5].** Approximate NN keeps retrieval fast at ~59k rows and scales further; normalisation makes cosine the natural metric.
6. **`row_hash` primary key for idempotent upsert.** Makes ingest safely re-runnable and deduplicates the source in one mechanism.

---

## 11. Running the project

Full instructions are in `README.md` and `docs/setup.md` (which includes a troubleshooting table). 

#### Running the tests

The unit tests need no Docker, DB or model:

```bash
pip install -r requirements-dev.txt
pytest
```

They cover the ingest cleaning/normalisation/filtering logic in `scripts/ingest.py`.

---

## 12. Limitations and known issues

1. **Quantitative retrieval evaluation is preliminary.** A script now reports precision@k / success@k / MRR over the real corpus (§9.2), but the evidence is limited. The labelled set is small (9 queries) with approximate, keyword-based relevance, recall@k is not meaningful under the current broad relevance definitions, and suggestion quality is still not measured against ground truth.
2. **Retrieval is style-sensitive.** Prose queries against code-heavy shorthand categories retrieve poorly (§9.2). A general-purpose embedding model is not tuned to this domain's vocabulary.
3. **No authentication on `/suggest`.** Anyone who can reach the port can query the service and its grounding data (§8). Needs an API key or similar before any non-trivial exposure.
4. **Default database credentials.** `kbox`/`kbox` from `.env.example` are fine for local dev and are mitigated by binding the DB to localhost only, but must be changed for any shared deployment.
5. **CPU latency.** Without a GPU, first-request and cold generations take tens of seconds. Managed with warm-up and a 300s timeout, but the UX depends on suitable hardware.
6. **Suggestion quality is bounded by the corpus and the 8B model.** For faults with no similar history, the model has little to ground on.
7. **Category labels are inconsistent in the source data.** The workbook's `category` values include case-variant duplicates (e.g. `Contact problem` / `Contact Problem` / `contact problem`) and ~1,040 blank entries.

---

## 13. Future work

1. **Fine-tune the embedding model** on the maintenance corpus so domain shorthand and prose paraphrases land near each other in vector space.
2. **Expand the evaluation harness** grow the labelled query→expected-case set (with tighter per-query relevant sets so recall@k becomes informative) and use it to measure the impact of the fine-tuning swap objectively (§9.5).
3. **Authentication and secret management** on the API and database before any wider rollout.
4. **Metadata-filtered retrieval**. Let `product_family` / `test_station` (already accepted in the request) constrain the similarity search, not just flavour the prompt.
5. **Feedback loop**. Let engineers mark whether a suggestion helped, building the labelled data. In case-based-reasoning terms [6], this is the *revise*/*retain* half of the cycle that the current system does not yet implement (§3.4).
6. **GPU-backed Ollama host** for production-grade latency.

---

## 14. Response to peer review feedback

The project was reviewed by two peers. Their feedback fell into code quality, evaluation, and documentation.

### 14.1 Adopted — code

- **Input validation on `/suggest`.** A reviewer noted `fault_description` was type-only, so empty, whitespace, or unbounded text reached the embedder. It is now length-bounded and rejects blank input with a clean `422` (§7).
- **Defensive Ollama handling.** The success path assumed a well-formed JSON body; a malformed reply now returns a descriptive `502` rather than an opaque `500`.
- **Embedding-dimension safety.** A reviewer observed that ingest created the table but never checked an existing table's vector dimension against `EMBEDDING_DIM`. Ingest now verifies this and fails fast with a clear message (§6.4).
- **Endpoint tests.** The suite previously covered only ingest cleaning; tests for `POST /suggest` (success, empty DB, DB unavailable, Ollama timeout / malformed response, input validation) were added (§9.4).
- **Clearer similarity label.** The frontend figure was relabelled from "% match" to "% similarity" so it is not read as a probability of correctness.

### 14.2 Adopted — evaluation

- **Quantitative retrieval harness.** Both reviewers identified the absence of a formal evaluation as the priority gap. A reproducible harness was built and run on the real corpus; precision@k / success@k / MRR are reported in §9.2, with the auto-vs-labelled gap quantifying the style-sensitivity previously described only qualitatively.
- **Distance threshold to filter weak matches.** A reviewer suggested suppressing low-similarity results. Investigating the distance distribution (`--dump-distances`) showed that relevant and non-relevant cases overlap heavily on this corpus (§9.2), so a threshold *cannot* improve precision here. Rather than filter, an optional `KBOX_MAX_DISTANCE` guard (off by default) was added that flags a response `low_confidence`.
- **Weak-match handling.** A reviewer suggested detecting when no sufficiently similar history exists (see §14.4 for how this was scoped).

### 14.3 Adopted — documentation

- Abstract and conclusion wording changed from "validated" to "tested" end to end, to match the qualitative-then-quantitative evidence actually presented.
- The idempotency / re-run description was corrected: the `row_hash` upsert is an in-place refresh, not a full rebuild (source-deleted rows remain) (§9.3).
- Inconsistent raw-row counts were standardised across the documentation.
- The unreadable Mermaid diagram was replaced with a rendered SVG (§5), a Background and related-work section with citations was added (§3), and a broken README code block was fixed.

### 14.4 Partially adopted / respectfully disagreed

- **Using distance to catch data-import issues.** A reviewer suggested using similarity to flag mis-imported or mis-categorised rows. This was not built as an automated check (out of scope for the timeframe), but a concrete instance of the underlying problem was found and is documented as a data-quality limitation (§12, item 7).

### 14.5 Acknowledged — deferred to future work

- **Metadata-filtered retrieval** (letting `product_family` / `test_station` constrain the search, not just the prompt) is deferred (§13, item 4).
- **A larger expert-labelled evaluation** with a human relevance study is designed and scheduled but not yet run; the protocol and a ready-to-run form generator are included (§9.5).
- **Authentication on `/suggest`** is acknowledged as a limitation for any wider-than-localhost deployment (§8, §12) and listed as future work (§13).

---

## 15. Conclusion

Knowledge Box turns a large, static maintenance workbook into a natural-language question-answering tool that surfaces the most relevant historical call-outs and a grounded suggested fix. Commercially sensitive data is kept local with the architecture small and honest.

It has been tested end to end on ~59k real indexed records, and its main weaknesses (the lack of a formal quantitative evaluation and the style-sensitivity of a general-purpose embedding model) are understood, documented, and mapped to 'next steps'.

---

## References

[1] P. Lewis *et al.*, "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks," in *Proc. Adv. Neural Inf. Process. Syst. (NeurIPS)*, vol. 33, 2020. https://arxiv.org/pdf/2005.11401

[2] V. Karpukhin *et al.*, "Dense Passage Retrieval for Open-Domain Question Answering," in *Proc. Conf. Empirical Methods Natural Lang. Process. (EMNLP)*, 2020, pp. 6769–6781. https://aclanthology.org/2020.emnlp-main.550.pdf

[3] N. Reimers and I. Gurevych, "Sentence-BERT: Sentence Embeddings using Siamese BERT-Networks," in *Proc. Conf. Empirical Methods Natural Lang. Process. Int. Joint Conf. Natural Lang. Process. (EMNLP-IJCNLP)*, 2019, pp. 3982–3992. https://aclanthology.org/D19-1410.pdf

[4] W. Wang, F. Wei, L. Dong, H. Bao, N. Yang, and M. Zhou, "MiniLM: Deep Self-Attention Distillation for Task-Agnostic Compression of Pre-Trained Transformers," in *Proc. Adv. Neural Inf. Process. Syst. (NeurIPS)*, vol. 33, 2020. https://arxiv.org/pdf/2002.10957

[5] Yu. A. Malkov and D. A. Yashunin, "Efficient and robust approximate nearest neighbor search using Hierarchical Navigable Small World graphs," *IEEE Trans. Pattern Anal. Mach. Intell.*, vol. 42, no. 4, pp. 824–836, 2020. https://arxiv.org/pdf/1603.09320

[6] A. Aamodt and E. Plaza, "Case-Based Reasoning: Foundational Issues, Methodological Variations, and System Approaches," *AI Commun.*, vol. 7, no. 1, pp. 39–59, 1994. https://www.researchgate.net/publication/225070522_Case-Based_Reasoning_Foundational_Issues_Methodological_Variations_and_System_Approaches

[7] K. Shuster, S. Poff, M. Chen, D. Kiela, and J. Weston, "Retrieval Augmentation Reduces Hallucination in Conversation," in *Findings Assoc. Comput. Linguistics: EMNLP*, 2021, pp. 3784–3803. https://aclanthology.org/2021.findings-emnlp.320.pdf


---

## Appendix A — Repository layout

```
scripts/ingest.py        Excel/CSV -> clean -> Postgres + embeddings
scripts/evaluate.py      Retrieval eval harness (labelled + auto modes; §9.2)
scripts/make_human_eval.py  Generates the human relevance-rating form (§9.5)
app/main.py              FastAPI service (POST /suggest, serves the frontend)
app/embeddings.py        Shared embedding wrapper (index-time and query-time)
app/static/index.html    Form-based frontend (fault input -> suggestion + cases)
tests/                   pytest suite: ingest cleaning + POST /suggest paths
docs/report.md           This report
docs/architecture.svg    Rendered system-architecture diagram (used in §5)
docs/setup.md            Detailed setup walkthrough + troubleshooting
docs/deployment.md       Shared-server (containerised) deployment guide
docs/evaluation.md       Evaluation harness usage
docs/eval/               Labelled query set + human-study protocol/form
data/sample.csv          Synthetic sample data (tracked in git)
data/raw/                Real workbook goes here (git-ignored)
docker-compose.yml       Postgres/pgvector + Ollama (+ app under `full` profile)
Dockerfile               Container image for the API service
```

## Appendix B — `POST /suggest` reference

Request:

```json
{
  "fault_description": "Unit will not power on, seems to have a blown fuse",
  "product_family": "ProLine-X",   // optional
  "test_station": "EOL-1",          // optional
  "top_k": 5                          // 1..20, default 5
}
```

Response:

```json
{
  "suggested_action": "Replace the blown control-board fuse (e.g. F3) and retest ...",
  "supporting_cases": [
    {
      "fault_description": "Unit dead on power up blown fuse F3 on control board",
      "remedial_action": "Replaced F3 2A fuse on control board and retested",
      "category": "Electrical",
      "bay": "Bay 3", "cell": "Cell 2",
      "product_family": "ProLine-X", "test_station": "EOL-1",
      "distance": 0.04
    }
  ],
  "low_confidence": false,        // true only if KBOX_MAX_DISTANCE is set and exceeded
  "confidence_note": null          // explanatory text when low_confidence is true
}
```

`distance` is cosine distance (lower = more similar); the frontend renders it as `round((1 - distance) * 100)`% similarity. `low_confidence` is the optional weak-match guard (§7, §9.2): unset `KBOX_MAX_DISTANCE` leaves it always `false`.

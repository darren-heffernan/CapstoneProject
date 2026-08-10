# Retrieval evaluation

How to measure and interpret the quality of Knowledge Box's retrieval step. This complements the qualitative findings in [report.md](report.md) §9 with reproducible quantitative numbers.

`scripts/evaluate.py` evaluates retrieval only: it reuses the same embedding wrapper (`app/embeddings.py`) and the same cosine-distance query as the live `/suggest` endpoint. The metrics describe the real retriever rather than a stand-in. It does not score the LLM's generated text; suggestion quality remains a human judgement.

## What "relevant" means

Retrieval metrics need a definition of which retrieved rows count as relevant.

### Labelled mode (default) — precise, needs a little labelling

You author a small query set in [eval/labelled_queries.json](eval/labelled_queries.json). Each entry is a free-text `query` plus a relevance spec:

```json
{
  "query": "light curtain on the cell keeps tripping and halting the line",
  "category": "Mechanical Failure",
  "relevant_any_keywords": ["curtain", "light curtain"]
}
```

A retrieved row is relevant if its `category` matches and its fault/remedial text contains at least one keyword.

The query set targets the real workbook's category taxonomy (`Contact problem`, `Electronic equipment failure`, `Software issue`, `Mechanical Failure`, …). Any query that matches zero rows flags labels that don't fit the data you ingested. If you evaluate against `data/sample.csv` instead, retarget the labels to its categories. Point the harness at an alternative file with `--labelled path/to/queries.json`.

### Auto / leave-one-out mode — zero labelling, coarse relevance

`--auto N` samples `N` real indexed rows, uses each row's own `fault_description` as a query (excluding that row from its own results), and treats other rows sharing its category as relevant. Category is a coarse relevance signal (e.g. two unrelated `Electrical` faults count as "relevant" to each other) so in auto mode the trustworthy metrics are **precision@k**, **success@k** and **MRR**.

## Metrics

For each query, at each cut-off `k`:

- **Precision@k**: fraction of the top-k retrieved rows that are relevant.
- **Recall@k**: relevant rows in the top-k divided by all relevant rows in the corpus.
- **Success@k**: (hit rate)  1 if at least one relevant row is in the top-k.
- **MRR**: reciprocal rank of the first relevant row (rank-sensitive: rewards putting a relevant case first).

Reported values are the mean across all queries.

## Running it

Needs the full stack up.

```bash
# Labelled mode (default query set)
python scripts/evaluate.py

# Auto leave-one-out over 200 sampled rows, reproducible
python scripts/evaluate.py --auto 200 --seed 0.42

# Choose cut-offs, and save machine-readable output
python scripts/evaluate.py --k 1 3 5 10 --json-out eval-results.json

# Use a custom labelled query file instead of the default
python scripts/evaluate.py --labelled docs/eval/my_queries.json

# Inspect the distance distribution to choose a weak-match threshold
python scripts/evaluate.py --auto 200 --dump-distances          # print summary
python scripts/evaluate.py --auto 200 --dump-distances dist.csv # also write CSV
```

Example of the printed layout:

```
Knowledge Box — retrieval evaluation (labelled mode)
Corpus: 59,063 indexed records   Queries: 9
----------------------------------------------------------
  k   Precision@k    Recall@k   Success@k
----------------------------------------------------------
  1         0.xxx       0.xxx       0.xxx
  3         0.xxx       0.xxx       0.xxx
  5         0.xxx       0.xxx       0.xxx
 10         0.xxx       0.xxx       0.xxx
----------------------------------------------------------
MRR: 0.xxx
```

## Interpreting the numbers

- **High precision@k, high success@1**: the retriever reliably surfaces same-kind cases at the top. This is the outcome the qualitative testing in §9.2 suggested for log-style queries.
- **Precision that falls as k grows**: later results are less similar. Watch success@k instead; if success@5 is high, a relevant case is almost always on screen even when not ranked first.
- **A gap between auto mode and labelled mode**: labelled queries are natural-language paraphrases, so weaker labelled-mode scores would confirm the style-sensitivity documented in §9.2 (prose queries vs. terse component-code shorthand) and strengthen the case for the fine-tuning work in §13.

## Choosing a weak-match threshold (`--dump-distances`)

`--dump-distances` prints the cosine-distance distribution of the retrieved rows, split into relevant and non-relevant, at the p10–p90 percentiles (and optionally writes every `(distance, relevant)` pair to CSV). It is the evidence for whether an optional weak-match cutoff makes sense.

On the real corpus the two distributions overlap heavily (both median ≈ 0.000; see report §9.2), because identical terse strings recur across categories. Therefore distance cannot separate relevant from irrelevant here. The only use of a threshold is to detect the "no similar history at all" case, which is what the optional `KBOX_MAX_DISTANCE` guard in `app/main.py` does (off by default; flags a response `low_confidence`).

## Limitations

- Retrieval only; generation quality is not scored here.
- Category-level relevance (auto mode) is coarse and can both over- and under-count; labelled mode with keywords is tighter but only as good as the labels.
- The labelled set is small. Growing it (ideally from real engineer feedback, per §13) is the path to more reliable numbers.
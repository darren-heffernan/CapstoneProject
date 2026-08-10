# Human relevance study

A small expert-judged study of retrieval quality, to complement `scripts/evaluate.py`. It exists because the only machine-checkable relevance label available (`category`) is too noisy to grade retrieval on its own (see report §9.2 and §12). An engineer deciding "is this retrieved case useful for this fault?" is the missing ground truth.


## Method

1. **Sample.** Draw N real indexed faults at random (default N = 20; use `--seed` for a reproducible draw). Each sampled fault's own text is the query.
2. **Retrieve.** For each query, fetch the top-k nearest historical cases (default k = 5).
3. **Rate.** An engineer marks each retrieved case **useful / not useful** i.e. relevant or not to the current query fault.

Generate the form with:

```bash
python scripts/make_human_eval.py --n 20 --k 5 --seed 0.42
```

This writes a single self-contained `docs/eval/human_eval_form.html`. The engineer opens it in any browser, ticks the boxes (~15 min) and clicks **Download ratings (CSV)**.

## Metrics

From the returned CSV (one row per query×case, with `useful` in {0,1}):

- **Usefulness rate** = useful cases ÷ total cases shown. A human analogue of precision@k.
- **Success@k** = fraction of queries with at least one useful case in the top k.
- **First-useful rank** (optional) = mean reciprocal rank of the first useful case, for a position-sensitive number.

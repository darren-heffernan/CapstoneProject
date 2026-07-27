# Decisions log

## 2026-07-13 — Project scaffolded

- **Self-hosted only.** Postgres/pgvector and Ollama run via `docker-compose.yml`; no
  external LLM APIs anywhere in the pipeline. Two-machine workflow: laptop for
  code/config (git), desktop (or wherever Docker + GPU live) for the actual
  ingest/DB/model runtime.
- **Git scope.** Repo tracks code and config only. Raw workbooks, `.env`, virtualenvs,
  and model artifacts are git-ignored. `data/sample.csv` is the one exception — a
  small synthetic CSV kept in git so the schema and ingest pipeline are runnable
  without the real (anonymised) workbook.
- **Embedding wrapper.** All embedding calls go through a single wrapper function
  `app/embeddings.py` so the sentence-transformers model can later be swapped for a fine-tuned one without touching call sites.
- **Non-fault filtering.** Rows categorised as Changeover, Operator Error, No fault
  found, Preventative Maintenance, or Call out cancelled are filtered out at index
  time — they don't represent a fault-to-remedial-action pattern worth retrieving.
- **Sample data.** `data/sample.csv` includes deliberately recurring fault patterns
  (blown fuse, frozen software, sound-fail/RTV) across different bays/cells/dates so
  similarity retrieval has something to demonstrate, plus a few rows in each
  filtered-out category to prove the filter works.

## 2026-07-18 — Ingest + suggest implemented, validated against real workbook

- **Column mapping.** The real workbook's headers are messy (`Bay #`, `Product`,
  `Call out Fault \ Description`, `Time to resolve (mins)`). `scripts/ingest.py` normalises any
  header by stripping punctuation to underscores (handles `Bay #` → `bay`,
  `Time to resolve (mins)` → `time_to_resolve_mins` for free), then applies an
  explicit `COLUMN_ALIASES` map for renames punctuation-stripping can't infer
  (`Product` → `product_family`, the fault-description column → `fault_description`).
  Extend that dict, not the schema, if a future export uses different wording.
- **Non-fault filtering left unchanged.** The real category taxonomy is far more
  granular than the five categories filtered above, including near-duplicates of
  them (`Planned maintenance` alongside `Preventative Maintenance`) and mixed-content
  categories (`Cell set-up`, `New line set-up`) that contain genuine fault/fix
  narratives alongside pure setup logistics. Decision: don't expand the filter list.
  `Planned maintenance` reads as pure scheduled-maintenance content (same as
  `Preventative Maintenance`) but was kept in for consistency rather than special-cased;
  `Cell set-up`/`New line set-up` are kept because filtering the whole category would
  discard real troubleshooting content along with the noise. Revisit if retrieval
  quality suffers from the noise in practice.
- **Retrieval is style-sensitive.** Validated end-to-end against the real ~80k-row
  workbook (80,117 raw → 59,063 indexed after cleaning/filtering/dedup). Retrieval
  quality is strong when a query is phrased like the historical logs — including
  terse component-code shorthand (`"Failed on CR51"` matched near-identically,
  distance ≈0) — but weaker for natural-language paraphrases of code-heavy categories
  like `Contact problem`. Not fixed; just documented so it doesn't look like a bug.
- **Row-hash dedup confirmed correct.** The `row_hash` primary key in
  `maintenance_records` (hashing date/shift/bay/cell/fault_description/remedial_action)
  collapsed 253 rows during the real ingest. Spot-checked several collision groups —
  all were exact duplicate call-out entries in the source workbook, not distinct
  incidents losing data.

## 2026-07-18 — Frontend served by FastAPI, not a separate Flask app

- **No second framework.** The demo frontend (`app/static/index.html`) is a plain
  HTML/JS page served directly by the existing FastAPI app via `StaticFiles`, rather
  than standing up a separate Flask service. A second framework would mean another
  process, another port, and another Docker service.

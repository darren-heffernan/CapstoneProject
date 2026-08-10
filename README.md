# Knowledge Box

Self-hosted RAG microservice that suggests remedial actions for maintenance call-outs, grounded in an anonymised ~80k-row maintenance workbook.
Given a fault description, it retrieves similar historical faults (via pgvector similarity search over sentence-transformer embeddings) and asks a locally-served Ollama model to synthesise a suggested remedial action.

## Stack

- FastAPI (`app/`) serves `POST /suggest` and a small static frontend
- Postgres + pgvector stores maintenance rows and their embeddings
- sentence-transformers embed fault descriptions
- Ollama serves the LLM used to generate suggestions

## Quick start

1. Start the backing services (Postgres/pgvector + Ollama):

   ```bash
   docker compose up -d
   ```

2. Set up your environment:

   ```bash
   cp .env.example .env
   python -m venv .venv
   source .venv/bin/activate   # or .venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```

3. Pull the Ollama model referenced by `OLLAMA_MODEL` in `.env`:

   ```bash
   docker exec -it <ollama-container> ollama pull llama3.1:8b
   ```

4. Run ingest to build the embedded index. Point `RAW_DATA_PATH` in `.env` at the real workbook, or leave it unset to use `data/sample.csv`:

   ```bash
   python scripts/ingest.py
   ```

5. Run the API:

   ```bash
   uvicorn app.main:app --reload
   ```

   Then open `http://127.0.0.1:8000/` for the form-based frontend, or `http://127.0.0.1:8000/docs` for the interactive Swagger UI.

*For the detailed version of these steps (including troubleshooting for things like Docker Desktop not being started, Ollama cold-start timeouts, and messy real-world workbook column names) see [docs/setup.md](docs/setup.md).*

## Report

A full project write-up is in [docs/report.md](docs/report.md).

## Tests

The pure-Python ingest cleaning/normalisation logic is covered by a `pytest` suite that needs no Docker, database or model:

```bash
pip install -r requirements-dev.txt
pytest
```

## Evaluation

`scripts/evaluate.py` measures retrieval quality (precision@k / recall@k / success@k / MRR) using the same embedding wrapper and cosine-distance query as `/suggest`. Needs an ingested corpus (Machine B):

```bash
python scripts/evaluate.py            # labelled query set (docs/eval/)
python scripts/evaluate.py --auto 200 # auto leave-one-out over 200 rows
```

Methodology and interpretation: [docs/evaluation.md](docs/evaluation.md).

## Two-machine workflow

Code and config live in git; data and the DB/model runtime do not.

- **Machine A (e.g. laptop)**. Code and git version control. No raw data, no `.env`, no running containers required here.
- **Machine B (e.g. desktop with Docker/GPU)**. Where the pipeline runs.

Because the DB and models rebuild from `ingest.py` rather than being synced, either machine can be wiped and reconstructed from git + the raw workbook alone.

## Data handling

- Only `data/sample.csv` is tracked in git. Everything else under `data/` is git-ignored.
- Rows categorised as Changeover, Operator Error, No fault found, Preventative Maintenance, or Call out cancelled are filtered out at index time.
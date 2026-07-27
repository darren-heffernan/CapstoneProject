# Knowledge Box

RAG microservice that suggests remedial actions for maintenance call-outs,
grounded in an anonymised ~77k-row maintenance workbook. Given a fault
description, it retrieves similar historical faults (via pgvector similarity
search over sentence-transformer embeddings) and asks a locally-served Ollama
model to synthesise a suggested remedial action — entirely self-hosted, no
external LLM APIs.

## Stack

- FastAPI (`app/`) — serves `POST /suggest` and a small static frontend at `/`
- Postgres + pgvector — stores maintenance rows and their embeddings
- sentence-transformers — embeds fault descriptions
- Ollama — serves the LLM used to generate suggestions

## Quick start

For the detailed version of these steps — including troubleshooting for
things like Docker Desktop not being started, Ollama cold-start timeouts, and
messy real-world workbook column names — see
[docs/setup.md](docs/setup.md).

1. **Start the backing services** (Postgres/pgvector + Ollama):

   ```bash
   docker compose up -d
   ```

2. **Set up your environment:**

   ```bash
   cp .env.example .env
   python -m venv .venv
   source .venv/bin/activate   # or .venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```

3. **Pull the Ollama model** referenced by `OLLAMA_MODEL` in `.env`:

   ```bash
   docker exec -it <ollama-container> ollama pull llama3.1:8b
   ```

4. **Run ingest** to build the embedded index. By default this uses
   `data/sample.csv` — the synthetic sample shipped with the repo — which is
   all you need to get the service running:

   ```bash
   python scripts/ingest.py

5. **Run the API:**

   ```bash
   uvicorn app.main:app --reload
   ```

   Then open `http://127.0.0.1:8000/` for the form-based frontend, or
   `http://127.0.0.1:8000/docs` for the interactive Swagger UI.

## Report

A full project write-up — problem, architecture, data pipeline, evaluation,
design decisions, limitations and future work — is in
[docs/report.md](docs/report.md).

## Tests

The pure-Python ingest cleaning/normalisation logic is covered by a `pytest`
suite that needs no Docker, database or model:

```bash
pip install -r requirements-dev.txt
pytest
```


## Data handling

- Only `data/sample.csv` (synthetic) is tracked in git. Everything else under
  `data/` — the real workbook, exports, raw dumps — is git-ignored.
- Rows categorised as Changeover, Operator Error, No fault found, Preventative
  Maintenance, or Call out cancelled are filtered out at index time; see
  [docs/decisions.md](docs/decisions.md).

## Project layout

```
scripts/ingest.py    Excel/CSV -> clean -> Postgres + embeddings
app/main.py          FastAPI service (POST /suggest, serves the frontend)
app/embeddings.py    Shared embedding wrapper (index-time and query-time)
app/db.py            Shared Postgres connection helper
app/static/index.html  Form-based frontend (fault input -> suggestion + cases)
tests/               pytest suite for the ingest cleaning logic
docs/report.md       Full project report
docs/setup.md        Detailed setup walkthrough + troubleshooting
docs/deployment.md   Shared-server (containerised) deployment guide
docs/decisions.md    Append-only decisions log
data/sample.csv      Synthetic sample data (tracked in git)
data/raw/            Real workbook goes here (git-ignored)
```

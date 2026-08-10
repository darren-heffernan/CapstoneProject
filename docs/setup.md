# Setup from scratch

Step-by-step instructions for getting Knowledge Box running on a fresh machine (Windows, with Docker Desktop). This is the detailed version of the README's quick start. It also covers troubleshooting.

## Prerequisites

- Docker Desktop (with the WSL2/Linux engine). Must actually be running, not just installed. `docker compose` fails with a `dockerDesktopLinuxEngine` pipe error if the Desktop app isn't started yet.
- Python 3.11+ (this project has been run successfully on 3.14).
- Enough disk space for the Ollama model (~5GB for `llama3.1:8b`) and the Postgres/pgvector data volume.

## 1. Clone and configure environment

```bash
git clone <repo-url>
cd kb
cp .env.example .env
```

Open `.env` and check the values match what you need:

- `POSTGRES_*`: only need to change these if you're not using the `docker-compose.yml` defaults.
- `OLLAMA_MODEL`: defaults to `llama3.1:8b`. Change if you want a different self-hosted model.
- `RAW_DATA_PATH`: path to the real maintenance workbook. Non-existent path (the default) falls back to `data/sample.csv`.

## 2. Start the backing services

Make sure Docker Desktop is actually running first (open the app, wait for it to say "Engine running"), then:

```bash
docker compose up -d
```

First run pulls two images (~3GB for Ollama, smaller for pgvector), so this can take a few minutes. Verify both containers are healthy:

```bash
docker compose ps
```

You should see `kb-db-1` (healthy) and `kb-ollama-1` (up). Container names follow docker compose's `<folder>-<service>-1` pattern. *If you cloned the repo into a differently-named folder, adjust commands below accordingly.*

## 3. Set up the Python environment

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
# or: .venv\Scripts\activate    # Windows PowerShell/cmd
# or: source .venv/bin/activate # macOS/Linux

pip install -r requirements.txt
```

This installs `sentence-transformers` (and its `torch` dependency) alongside FastAPI, psycopg, pgvector, etc.

## 4. Pull the Ollama model

```bash
docker exec kb-ollama-1 ollama pull llama3.1:8b
```

Check it landed:

```bash
docker exec kb-ollama-1 ollama list
```

## 5. Run the ingest pipeline

```bash
python scripts/ingest.py
```

On first run this also downloads the `sentence-transformers/all-MiniLM-L6-v2` embedding model from Hugging Face. You'll see log lines like:

```
INFO Loaded <N> raw rows from ...
INFO Dropped <N> rows with missing fault_description/remedial_action
INFO Filtered <N> non-fault rows by category; <N> remain
INFO Embedding <N> fault descriptions...
INFO Upserted <N> rows into maintenance_records
```

With the default `data/sample.csv` this takes a few seconds. Against a real ~80k-row workbook, embedding takes a few minutes on CPU.


Verify data landed:

```bash
docker exec kb-db-1 psql -U kbox -d knowledgebox -c "SELECT count(*) FROM maintenance_records;"
```

## 6. Run the API

```bash
uvicorn app.main:app --reload
```

Startup warms up both the embedding model and Ollama (so the first real request isn't slow). Watch the logs for `Application startup complete`. Confirm it's up:

```bash
curl http://127.0.0.1:8000/docs
```

## 7. Test it

```bash
curl -X POST http://127.0.0.1:8000/suggest \
  -H "Content-Type: application/json" \
  -d '{"fault_description": "Unit will not power on, seems to have a blown fuse", "top_k": 3}'
```

You should get back a `suggested_action` plus `supporting_cases`. The first request against a freshly started Ollama model can still take 30-60+ seconds on CPU-only hardware even after warmup.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `docker compose up` fails with a `dockerDesktopLinuxEngine` pipe error | Docker Desktop app isn't running | Start Docker Desktop, wait for the engine to come up, retry |
| `operator does not exist: vector <=> double precision[]` | Postgres can't infer a bare parameter's type in a similarity query | Already handled in `app/main.py` via an explicit `%s::vector` cast — if you see this in new code, add the same cast |
| `/suggest` request times out / Ollama load gets aborted | First model load on CPU is slow; a client giving up early cancels the load entirely | `app/main.py` warms Ollama up at startup and uses a 300s timeout; if you still see this, the model may need even longer on very constrained hardware |
| Port 8000 already in use when restarting `uvicorn` | A previous server instance is still running | Find and kill it: `netstat -ano \| grep ':8000'` then `taskkill //F //PID <pid>` (Windows) |
| `huggingface_hub` symlinks warning on Windows | Windows needs Developer Mode or admin rights for symlinked model cache | Harmless — ignore, or enable Developer Mode to silence it |
| A real workbook's required column (e.g. `fault_description`) isn't found | Real-world header wording doesn't match the schema | Check/extend `COLUMN_ALIASES` in `scripts/ingest.py` |

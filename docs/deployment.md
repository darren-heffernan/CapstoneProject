# Deploying to a shared server

This covers running the full stack — Postgres/pgvector, Ollama, *and* the
FastAPI app — as containers on a Windows server so colleagues on the office
network can reach `POST /suggest`.

> Not needed for Peer Review. This doc is only for standing the service up on a shared server.

## What's different from local dev

Locally, `docker compose up -d` starts only Postgres and Ollama, and the app
runs via a Python venv (`uvicorn app.main:app --reload`) directly on your
machine. On a shared server, the app is containerized too, using the
`full` Compose profile so it doesn't change local dev's behavior:

```
docker compose up -d                 # local dev: db + ollama only, as before
docker compose --profile full up -d  # server: db + ollama + app, all containerized
```

**Security note on port exposure.** `db` (5432) and `ollama` (11434) are
bound to `127.0.0.1` only — not reachable from other machines on the
network. Only the `app` service's port 8000 is published network-wide. This
matters because Postgres ships with the default `kbox`/`kbox` credentials
from `.env.example`; publishing it to the LAN would let anyone on the office
network connect to it directly, or drive the Ollama model directly and burn
CPU.

**`POST /suggest` itself has no authentication.** Once port 8000 is open on
the LAN, anyone who can reach `ei-oms:8000` can query it — including whatever
maintenance history it's grounded in. Decide whether that's acceptable for
your office network before opening the firewall; if not, this needs an
API-key check or similar added to `app/main.py` before going further.

## 1. Prerequisites on the server

- Docker Desktop for Windows, running (same as local setup).
- Git, to clone the repo.

## 2. Clone and configure

```powershell
git clone <repo-url>
cd kb
copy .env.example .env
```

Edit `.env` for the server:
- Change `POSTGRES_PASSWORD` from the default.
- `RAW_DATA_PATH` should point at the real workbook,
  under `data/raw/`.

## 3. Build and start everything

```powershell
docker compose --profile full up -d --build
```

First run builds the app image (installs `torch` + `sentence-transformers`) and pulls the Postgres/Ollama images if not already
present.

## 4. Pull the Ollama model

```powershell
docker exec kb-ollama-1 ollama pull llama3.1:8b
```

## 5. Run ingest

Drop the real workbook under `data/raw/` on the server first, matching
`RAW_DATA_PATH` in `.env`, then run ingest *inside* the app container:

```powershell
docker compose --profile full run --rm app python scripts/ingest.py
```

Note: on its first run the app container needs outbound internet to fetch the
`all-MiniLM-L6-v2` embedding model from Hugging Face.

Verify:

```powershell
docker exec kb-db-1 psql -U kbox -d knowledgebox -c "SELECT count(*) FROM maintenance_records;"
```

## 6. Open the firewall

```powershell
New-NetFirewallRule -DisplayName "Knowledge Box API" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

## 7. Verify from another machine on the network

```
http://ei-oms:8000/docs
```


## Restarting / updating

```powershell
git pull
docker compose --profile full up -d --build
```

The `app` container rebuilds from source; `db` and `ollama` keep their data
in named volumes (`pgdata`, `ollama`) across restarts, so you don't need to
re-run ingest or re-pull the model unless the data or model itself changes.

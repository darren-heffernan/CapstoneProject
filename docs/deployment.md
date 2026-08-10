# Deploying to a shared server (e.g. ei-oms)

This covers running the full stack as containers on a Windows server so colleagues on the office network can reach `POST /suggest`.

## What's different from local dev

Locally, `docker compose up -d` starts only Postgres and Ollama, and the app runs via a Python venv (`uvicorn app.main:app --reload`) directly on your machine. On a shared server, the app is containerized too, using the `full` Compose profile so it doesn't change local dev's behavior:

```
docker compose up -d                 # local dev: db + ollama only, as before
docker compose --profile full up -d  # server: db + ollama + app, all containerized
```

**Security note on port exposure.** `db` (5432) and `ollama` (11434) are bound to `127.0.0.1` only. Only the `app` service's port 8000 is published network-wide. This matters because Postgres ships with the default `kbox`/`kbox` credentials from `.env.example`. Publishing it to the LAN would let anyone on the office network connect to it directly, or drive the Ollama model directly and burn CPU.

**`POST /suggest` itself has no authentication.** Once port 8000 is open on the LAN, anyone who can reach `ei-oms:8000` can query it. This includes whatever maintenance history it's grounded in. Decide whether that's acceptable for your office network before opening the firewall; if not, this needs an API-key check or similar added to `app/main.py` before going further.

## 1. Prerequisites on the server

- Docker Desktop for Windows.
- Git, to clone the repo.

## 2. Clone and configure

```powershell
git clone <repo-url>
cd kb
copy .env.example .env
```

Edit `.env` for the server:
- Change `POSTGRES_PASSWORD` from the default.
- `RAW_DATA_PATH` should point at the real workbook under `data/raw/`.

## 3. Build and start everything

```powershell
docker compose --profile full up -d --build
```

First run builds the app image (installs `torch` + `sentence-transformers`) and pulls the Postgres/Ollama images if not already present.

## 4. Pull the Ollama model

```powershell
docker exec kb-ollama-1 ollama pull llama3.1:8b
```

## 5. Run ingest

Run `ingest.py` inside the app container (it already has `scripts/` and a live mount of `./data`):

```powershell
docker compose --profile full run --rm app python scripts/ingest.py
```

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


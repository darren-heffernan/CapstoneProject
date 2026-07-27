"""FastAPI service exposing POST /suggest.

Intended behaviour:

1. Accept a fault description (and optionally product_family / test_station
   context) in the request body.
2. Embed the incoming fault description via the shared embedding wrapper
   (same one used by scripts/ingest.py, so query-time and index-time
   embeddings stay consistent).
3. Run a pgvector similarity search against the indexed maintenance rows to
   retrieve the top-k most similar historical faults and their recorded
   remedial actions.
4. Build a grounding prompt from the retrieved rows and send it to the
   locally-served Ollama model (``OLLAMA_HOST`` / ``OLLAMA_MODEL``) to
   synthesise a suggested remedial action.
5. Return the suggested action along with the supporting historical rows.

"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pgvector.psycopg import register_vector
from psycopg.rows import DictRow, dict_row
from pydantic import BaseModel, Field

from app.db import connect
from app.embeddings import embed_text

load_dotenv()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_TIMEOUT_SECONDS = 300


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    embed_text("")  # warm the embedding model so the first real request isn't slow
    try:
        requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": "", "stream": False},
            timeout=OLLAMA_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException:
        pass  # Ollama may not be reachable yet; /suggest will surface the real error
    yield


app = FastAPI(title="Knowledge Box", lifespan=lifespan)


class SuggestRequest(BaseModel):
    fault_description: str
    product_family: str | None = None
    test_station: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class RetrievedCase(BaseModel):
    fault_description: str
    remedial_action: str
    category: str | None
    bay: str | None
    cell: str | None
    product_family: str | None
    test_station: str | None
    distance: float


class SuggestResponse(BaseModel):
    suggested_action: str
    supporting_cases: list[RetrievedCase]


def get_db() -> Iterator[psycopg.Connection]:
    conn = connect()
    register_vector(conn)
    try:
        yield conn
    finally:
        conn.close()


def _retrieve_similar_cases(
    conn: psycopg.Connection, query_vector: list[float], top_k: int
) -> list[DictRow]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT fault_description, remedial_action, category, bay, cell,
                   product_family, test_station, embedding <=> %s::vector AS distance
            FROM maintenance_records
            ORDER BY distance
            LIMIT %s
            """,
            (query_vector, top_k),
        )
        return cur.fetchall()


def _build_prompt(request: SuggestRequest, cases: list[DictRow]) -> str:
    context = "\n\n".join(
        f"{i}. Fault: {case['fault_description']}\n   Remedial action taken: {case['remedial_action']}"
        for i, case in enumerate(cases, start=1)
    )

    extra_context = ""
    if request.product_family:
        extra_context += f"\nProduct family: {request.product_family}"
    if request.test_station:
        extra_context += f"\nTest station: {request.test_station}"

    return (
        "You are a maintenance engineer assistant. A new fault has been reported on a "
        "production line. Below are similar historical faults and the remedial actions "
        "that resolved them. Suggest a concise remedial action for the new fault, "
        "grounded in these historical resolutions.\n\n"
        f"Similar historical faults:\n{context}\n\n"
        f"New fault: {request.fault_description}{extra_context}\n\n"
        "Suggested remedial action:"
    )


def _generate_suggestion(prompt: str) -> str:
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=OLLAMA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not reach Ollama at {OLLAMA_HOST}: {exc}"
        ) from exc
    return response.json()["response"].strip()


@app.post("/suggest", response_model=SuggestResponse)
def suggest(
    request: SuggestRequest, conn: psycopg.Connection = Depends(get_db)
) -> SuggestResponse:
    query_vector = embed_text(request.fault_description)
    cases = _retrieve_similar_cases(conn, query_vector, request.top_k)

    if not cases:
        raise HTTPException(
            status_code=404,
            detail="No indexed maintenance records found; run scripts/ingest.py first.",
        )

    prompt = _build_prompt(request, cases)
    suggested_action = _generate_suggestion(prompt)

    return SuggestResponse(
        suggested_action=suggested_action,
        supporting_cases=[RetrievedCase(**case) for case in cases],
    )


# Mounted last so it only catches requests no route above it matched.
app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True), name="static")

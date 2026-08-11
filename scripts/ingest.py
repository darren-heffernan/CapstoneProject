"""Ingest pipeline: raw maintenance workbook -> cleaned rows -> Postgres + embeddings.

Intended behaviour:

1. Load the source workbook/CSV from ``RAW_DATA_PATH`` (falling back to
   ``data/sample.csv`` if unset), using pandas/openpyxl.
2. Clean the data: normalise column names, parse dates, drop rows with missing
   ``fault_description`` or ``remedial_action``.
3. Filter out non-fault categories at index time (Changeover, Operator Error,
   No fault found, Preventative Maintenance, Call out cancelled) — see docs/decisions.md.
4. Embed each row's fault description via the shared embedding wrapper
   (single choke point so the model can later be swapped for a fine-tuned
   one without touching call sites).
5. Create the pgvector-backed table (if not present) and upsert rows +
   embeddings into Postgres, connecting via the ``POSTGRES_*`` settings in
   ``.env``.

Run directly: ``python scripts/ingest.py``. Idempotent — re-running should rebuild the index from source without requiring a fresh database.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

# Make ``app`` importable when this script is run directly (its directory,
# not the repo root, is what Python puts on sys.path in that case).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.embeddings import EMBEDDING_DIM, embed_texts  # noqa: E402

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DATA_PATH = REPO_ROOT / "data" / "sample.csv"

NON_FAULT_CATEGORIES = {
    "changeover",
    "operator error",
    "no fault found",
    "preventative maintenance",
    "call out cancelled",
}

COLUMN_ALIASES = {
    "product": "product_family",
    "call_out_fault_description": "fault_description",
}

REQUIRED_COLUMNS = ["fault_description", "remedial_action", "category"]
OPTIONAL_COLUMNS = [
    "date",
    "shift",
    "bay",
    "cell",
    "product_family",
    "test_station",
    "time_to_resolve_mins",
]


def _resolve_source_path() -> Path:
    raw_path = os.getenv("RAW_DATA_PATH", "").strip()
    if not raw_path:
        logger.info("RAW_DATA_PATH not set; using sample data at %s", SAMPLE_DATA_PATH)
        return SAMPLE_DATA_PATH
    path = Path(raw_path)
    if not path.exists():
        logger.warning(
            "RAW_DATA_PATH=%s does not exist; falling back to sample data at %s",
            path,
            SAMPLE_DATA_PATH,
        )
        return SAMPLE_DATA_PATH
    return path


def _load_raw(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, engine="openpyxl")
    else:
        df = pd.read_csv(path)
    logger.info("Loaded %d raw rows from %s", len(df), path)
    return df


def _clean_column_name(col: str) -> str:
    # Collapse any run of non-alphanumeric characters (spaces, #, \, (), -, etc.)
    # to a single underscore so headers like "Bay #" or "Time to resolve (mins)"
    # normalise the same way regardless of the exact punctuation used.
    cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", str(col)).strip("_").lower()
    return COLUMN_ALIASES.get(cleaned, cleaned)


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=_clean_column_name)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Source data is missing required column(s) {missing}. "
            f"Columns found: {list(df.columns)}"
        )
    for col in OPTIONAL_COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["fault_description"] = df["fault_description"].astype("string").str.strip()
    df["remedial_action"] = df["remedial_action"].astype("string").str.strip()
    df["category"] = df["category"].astype("string").str.strip()

    before = len(df)
    df = df.dropna(subset=["fault_description", "remedial_action"])
    df = df[(df["fault_description"] != "") & (df["remedial_action"] != "")]
    logger.info("Dropped %d rows with missing fault_description/remedial_action", before - len(df))

    before = len(df)
    df = df[~df["category"].str.lower().isin(NON_FAULT_CATEGORIES)]
    logger.info("Filtered %d non-fault rows by category; %d remain", before - len(df), len(df))

    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["time_to_resolve_mins"] = pd.to_numeric(df["time_to_resolve_mins"], errors="coerce")

    return df.reset_index(drop=True)


def _row_hash(record: dict) -> str:
    key = "|".join(
        str(record.get(col, "")) for col in ("date", "shift", "bay", "cell", "fault_description", "remedial_action")
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _clean_value(value):
    if pd.isna(value):
        return None
    if isinstance(value, np.generic):
        return value.item()
    return value


def _row_values(record: dict) -> tuple:
    ttr = _clean_value(record.get("time_to_resolve_mins"))
    return (
        _row_hash(record),
        _clean_value(record.get("date")),
        _clean_value(record.get("shift")),
        _clean_value(record.get("bay")),
        _clean_value(record.get("cell")),
        _clean_value(record.get("product_family")),
        _clean_value(record.get("test_station")),
        _clean_value(record.get("fault_description")),
        _clean_value(record.get("remedial_action")),
        int(ttr) if ttr is not None else None,
        _clean_value(record.get("category")),
    )


def _connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        user=os.getenv("POSTGRES_USER", "kbox"),
        password=os.getenv("POSTGRES_PASSWORD", "kbox"),
        dbname=os.getenv("POSTGRES_DB", "knowledgebox"),
    )


def _ensure_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS maintenance_records (
                row_hash TEXT PRIMARY KEY,
                date DATE,
                shift TEXT,
                bay TEXT,
                cell TEXT,
                product_family TEXT,
                test_station TEXT,
                fault_description TEXT NOT NULL,
                remedial_action TEXT NOT NULL,
                time_to_resolve_mins INTEGER,
                category TEXT,
                embedding VECTOR({EMBEDDING_DIM}) NOT NULL
            )
            """
        )
        cur.execute(
            """
            SELECT atttypmod FROM pg_attribute
            WHERE attrelid = 'maintenance_records'::regclass AND attname = 'embedding'
            """
        )
        existing_dim = cur.fetchone()[0]
        if existing_dim != EMBEDDING_DIM:
            raise ValueError(
                f"Existing maintenance_records.embedding column has dimension "
                f"{existing_dim}, but EMBEDDING_DIM={EMBEDDING_DIM}. Align "
                f"EMBEDDING_DIM with the schema (or drop the table to rebuild) "
                f"before re-ingesting."
            )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS maintenance_records_embedding_idx
            ON maintenance_records USING hnsw (embedding vector_cosine_ops)
            """
        )
    conn.commit()
    register_vector(conn)


def _upsert(conn: psycopg.Connection, df: pd.DataFrame, embeddings: list[list[float]]) -> None:
    records = df.to_dict("records")
    rows = [(*_row_values(record), embedding) for record, embedding in zip(records, embeddings)]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO maintenance_records (
                row_hash, date, shift, bay, cell, product_family, test_station,
                fault_description, remedial_action, time_to_resolve_mins, category, embedding
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (row_hash) DO UPDATE SET
                date = EXCLUDED.date,
                shift = EXCLUDED.shift,
                bay = EXCLUDED.bay,
                cell = EXCLUDED.cell,
                product_family = EXCLUDED.product_family,
                test_station = EXCLUDED.test_station,
                fault_description = EXCLUDED.fault_description,
                remedial_action = EXCLUDED.remedial_action,
                time_to_resolve_mins = EXCLUDED.time_to_resolve_mins,
                category = EXCLUDED.category,
                embedding = EXCLUDED.embedding
            """,
            rows,
        )
    conn.commit()
    logger.info("Upserted %d rows into maintenance_records", len(rows))


def main() -> None:
    source_path = _resolve_source_path()
    df = _load_raw(source_path)
    df = _normalise_columns(df)
    df = _clean(df)

    if df.empty:
        logger.warning("No rows left to index after cleaning/filtering; nothing to do")
        return

    logger.info("Embedding %d fault descriptions...", len(df))
    embeddings = embed_texts(df["fault_description"].tolist())

    conn = _connect()
    try:
        _ensure_schema(conn)
        _upsert(conn, df, embeddings)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

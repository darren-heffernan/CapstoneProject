"""Shared Postgres connection helper.

Used by both the offline ingest path (``scripts/ingest.py``) and the online query path (``app/main.py``)
"""

from __future__ import annotations

import os

import psycopg


def connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        user=os.getenv("POSTGRES_USER", "kbox"),
        password=os.getenv("POSTGRES_PASSWORD", "kbox"),
        dbname=os.getenv("POSTGRES_DB", "knowledgebox"),
    )

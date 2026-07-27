"""Shared embedding wrapper.

Turns text into vectors. Used by both ``scripts/ingest.py`` (index-time) and ``app/main.py`` (query-time) so the two stay consistent
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

load_dotenv()

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "384"))

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        model = SentenceTransformer(EMBEDDING_MODEL)
        actual_dim = model.get_embedding_dimension()
        if actual_dim != EMBEDDING_DIM:
            raise ValueError(
                f"EMBEDDING_DIM={EMBEDDING_DIM} does not match "
                f"{EMBEDDING_MODEL}'s actual dimension ({actual_dim}); update .env"
            )
        _model = model
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts, returning one normalised vector per text."""
    model = _get_model()
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=len(texts) > 100)
    return vectors.tolist()


def embed_text(text: str) -> list[float]:
    """Embed a single text (used at query time)."""
    return embed_texts([text])[0]

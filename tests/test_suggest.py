"""Tests for the ``POST /suggest`` endpoint in ``app/main.py``.

These exercise the request/response wiring and error paths.
"""

from __future__ import annotations

import psycopg
import pytest
import requests
from fastapi.testclient import TestClient

from app import main as app_main
from app.main import app, get_db

# A retrieved row shaped exactly like what ``_retrieve_similar_cases`` returns
# (every field ``RetrievedCase`` expects), so patched retrieval stays realistic.
SAMPLE_CASE = {
    "fault_description": "Barcode scanner not reading on final test",
    "remedial_action": "Reseated the USB cable and recalibrated the scanner",
    "category": "Electrical",
    "bay": "3",
    "cell": "A",
    "product_family": "SmokeAlarm",
    "test_station": "ST-1",
    "distance": 0.12,
}


class _FakeOllamaResponse:
    """Stand-in for a ``requests`` response from Ollama."""

    def __init__(self, *, json_raises: bool = False, payload: dict | None = None):
        self._json_raises = json_raises
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        if self._json_raises:
            raise ValueError("not valid JSON")
        return self._payload


@pytest.fixture
def client(monkeypatch):
    # Default to a DB dependency that yields a dummy connection; retrieval is
    # patched per-test so the connection is never actually used. Tests that
    # need a DB failure override this within the test.
    def _fake_db():
        yield None

    app.dependency_overrides[get_db] = _fake_db
    # Embedding is patched globally so no model is ever loaded.
    monkeypatch.setattr(app_main, "embed_text", lambda text: [0.0])
    test_client = TestClient(app, raise_server_exceptions=False)
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


def test_suggest_success(client, monkeypatch):
    monkeypatch.setattr(
        app_main, "_retrieve_similar_cases", lambda conn, vec, k: [dict(SAMPLE_CASE)]
    )
    monkeypatch.setattr(app_main, "_generate_suggestion", lambda prompt: "Reseat the cable.")

    resp = client.post("/suggest", json={"fault_description": "scanner dead"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["suggested_action"] == "Reseat the cable."
    assert len(body["supporting_cases"]) == 1
    assert body["supporting_cases"][0]["fault_description"] == SAMPLE_CASE["fault_description"]


def test_suggest_low_confidence_flagged_when_nearest_exceeds_threshold(client, monkeypatch):
    far_case = dict(SAMPLE_CASE, distance=0.6)
    monkeypatch.setattr(app_main, "_retrieve_similar_cases", lambda conn, vec, k: [far_case])
    monkeypatch.setattr(app_main, "_generate_suggestion", lambda prompt: "Check the cable.")
    monkeypatch.setattr(app_main, "MAX_DISTANCE", 0.45)

    resp = client.post("/suggest", json={"fault_description": "obscure one-off fault"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["low_confidence"] is True
    assert body["confidence_note"]


def test_suggest_not_low_confidence_when_disabled(client, monkeypatch):
    far_case = dict(SAMPLE_CASE, distance=0.6)
    monkeypatch.setattr(app_main, "_retrieve_similar_cases", lambda conn, vec, k: [far_case])
    monkeypatch.setattr(app_main, "_generate_suggestion", lambda prompt: "Check the cable.")
    monkeypatch.setattr(app_main, "MAX_DISTANCE", None)  # guard disabled (default)

    resp = client.post("/suggest", json={"fault_description": "obscure one-off fault"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["low_confidence"] is False
    assert body["confidence_note"] is None


def test_suggest_empty_database_returns_404(client, monkeypatch):
    monkeypatch.setattr(app_main, "_retrieve_similar_cases", lambda conn, vec, k: [])

    resp = client.post("/suggest", json={"fault_description": "scanner dead"})

    assert resp.status_code == 404


def test_suggest_database_unavailable_returns_500(client, monkeypatch):
    def _broken_db():
        raise psycopg.OperationalError("could not connect to server")
        yield  # pragma: no cover - keeps this a generator dependency

    app.dependency_overrides[get_db] = _broken_db

    resp = client.post("/suggest", json={"fault_description": "scanner dead"})

    assert resp.status_code == 500


def test_suggest_ollama_timeout_returns_502(client, monkeypatch):
    monkeypatch.setattr(
        app_main, "_retrieve_similar_cases", lambda conn, vec, k: [dict(SAMPLE_CASE)]
    )

    def _timeout(*args, **kwargs):
        raise requests.exceptions.Timeout("timed out")

    monkeypatch.setattr(app_main.requests, "post", _timeout)

    resp = client.post("/suggest", json={"fault_description": "scanner dead"})

    assert resp.status_code == 502


def test_suggest_ollama_non_json_returns_502(client, monkeypatch):
    monkeypatch.setattr(
        app_main, "_retrieve_similar_cases", lambda conn, vec, k: [dict(SAMPLE_CASE)]
    )
    monkeypatch.setattr(
        app_main.requests, "post", lambda *a, **k: _FakeOllamaResponse(json_raises=True)
    )

    resp = client.post("/suggest", json={"fault_description": "scanner dead"})

    assert resp.status_code == 502


def test_suggest_ollama_missing_response_field_returns_502(client, monkeypatch):
    monkeypatch.setattr(
        app_main, "_retrieve_similar_cases", lambda conn, vec, k: [dict(SAMPLE_CASE)]
    )
    monkeypatch.setattr(
        app_main.requests,
        "post",
        lambda *a, **k: _FakeOllamaResponse(payload={"unexpected": "shape"}),
    )

    resp = client.post("/suggest", json={"fault_description": "scanner dead"})

    assert resp.status_code == 502


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_suggest_rejects_blank_fault_description(client, blank):
    resp = client.post("/suggest", json={"fault_description": blank})

    assert resp.status_code == 422


def test_suggest_rejects_overlong_fault_description(client):
    resp = client.post("/suggest", json={"fault_description": "x" * 2001})

    assert resp.status_code == 422

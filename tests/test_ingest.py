"""Unit tests for the pure-Python cleaning/normalisation logic in
``scripts/ingest.py``.

These cover the parts most prone to silently breaking when a new real-world
workbook format shows up (messy headers, non-fault categories, blank rows).
They need no Docker, database or embedding model — only the ingest module's
own helpers are exercised.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import ingest


class TestCleanColumnName:
    def test_strips_punctuation_to_single_underscore(self):
        # Headers with no alias just get punctuation-stripped and lower-cased.
        assert ingest._clean_column_name("Bay #") == "bay"
        assert ingest._clean_column_name("Time to resolve (mins)") == "time_to_resolve_mins"
        assert ingest._clean_column_name("Test Station") == "test_station"

    def test_lowercases_and_trims_edges(self):
        assert ingest._clean_column_name("  Remedial Action  ") == "remedial_action"

    def test_applies_explicit_aliases(self):
        # "Product" normalises to "product", which the alias map renames.
        assert ingest._clean_column_name("Product") == "product_family"
        # The messy fault-description header normalises to the aliased key, then
        # the alias map renames it to the schema name in one step.
        assert ingest._clean_column_name("Call out Fault \\ Description") == "fault_description"


class TestNormaliseColumns:
    def test_renames_headers_and_fills_optional_columns(self):
        df = pd.DataFrame(
            {
                "Call out Fault \\ Description": ["x"],
                "Remedial Action": ["y"],
                "Category": ["Electrical"],
                "Product": ["ProLine-X"],
            }
        )
        out = ingest._normalise_columns(df)
        assert "fault_description" in out.columns
        assert "remedial_action" in out.columns
        assert "product_family" in out.columns  # aliased from "Product"
        # Optional columns absent from the source are created (as null).
        for col in ingest.OPTIONAL_COLUMNS:
            assert col in out.columns

    def test_missing_required_column_raises_with_helpful_message(self):
        df = pd.DataFrame({"fault_description": ["x"], "category": ["Electrical"]})
        with pytest.raises(ValueError) as exc:
            ingest._normalise_columns(df)
        # Names the missing column and lists what was found.
        assert "remedial_action" in str(exc.value)
        assert "Columns found" in str(exc.value)


def _minimal_df(rows: list[dict]) -> pd.DataFrame:
    """Build a frame that has already been through column normalisation."""
    frame = pd.DataFrame(rows)
    return ingest._normalise_columns(frame)


class TestClean:
    def test_drops_rows_with_blank_fault_or_remedial(self):
        df = _minimal_df(
            [
                {"fault_description": "blown fuse", "remedial_action": "replaced fuse", "category": "Electrical"},
                {"fault_description": "  ", "remedial_action": "did something", "category": "Electrical"},
                {"fault_description": "no fix logged", "remedial_action": "", "category": "Electrical"},
            ]
        )
        out = ingest._clean(df)
        assert len(out) == 1
        assert out.iloc[0]["fault_description"] == "blown fuse"

    def test_filters_non_fault_categories_case_insensitively(self):
        df = _minimal_df(
            [
                {"fault_description": "real fault", "remedial_action": "real fix", "category": "Electrical"},
                {"fault_description": "line swap", "remedial_action": "did changeover", "category": "Changeover"},
                {"fault_description": "nothing wrong", "remedial_action": "released", "category": "No Fault Found"},
                {"fault_description": "wrong profile", "remedial_action": "corrected", "category": "operator error"},
            ]
        )
        out = ingest._clean(df)
        assert list(out["category"]) == ["Electrical"]

    def test_coerces_bad_numeric_and_date_values_to_null(self):
        df = _minimal_df(
            [
                {
                    "fault_description": "f",
                    "remedial_action": "r",
                    "category": "Electrical",
                    "date": "not-a-date",
                    "time_to_resolve_mins": "not-a-number",
                }
            ]
        )
        out = ingest._clean(df)
        assert pd.isna(out.iloc[0]["date"])
        assert pd.isna(out.iloc[0]["time_to_resolve_mins"])


class TestRowHash:
    def test_is_deterministic(self):
        record = {
            "date": "2026-06-01",
            "shift": "Day",
            "bay": "Bay 3",
            "cell": "Cell 2",
            "fault_description": "blown fuse",
            "remedial_action": "replaced fuse",
        }
        assert ingest._row_hash(record) == ingest._row_hash(dict(record))

    def test_changes_when_content_changes(self):
        base = {
            "date": "2026-06-01",
            "shift": "Day",
            "bay": "Bay 3",
            "cell": "Cell 2",
            "fault_description": "blown fuse",
            "remedial_action": "replaced fuse",
        }
        changed = dict(base, remedial_action="replaced a different fuse")
        assert ingest._row_hash(base) != ingest._row_hash(changed)


class TestCleanValue:
    def test_nan_becomes_none(self):
        assert ingest._clean_value(np.nan) is None
        assert ingest._clean_value(pd.NA) is None

    def test_numpy_scalar_becomes_native_python(self):
        result = ingest._clean_value(np.int64(42))
        assert result == 42
        assert not isinstance(result, np.generic)

    def test_plain_value_passes_through(self):
        assert ingest._clean_value("Bay 3") == "Bay 3"

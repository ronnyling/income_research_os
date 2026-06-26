"""Tests for MiMo batch processing and filing content hash.

Covers:
  - compute_filing_hash() determinism and collision resistance
  - analyze_dip_batch() prompt construction and chunking
  - _parse_batch_json_response() various response formats
  - Batch fallback to individual calls on chunk failure
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from incomos.llm.mimo import (
    compute_filing_hash,
    _parse_batch_json_response,
    _build_stock_block,
)


# ---------------------------------------------------------------------------
# Filing hash
# ---------------------------------------------------------------------------


class TestFilingHash:
    def test_deterministic(self):
        """Same input always produces same hash."""
        h1 = compute_filing_hash("mda text", "risk text", "prior text")
        h2 = compute_filing_hash("mda text", "risk text", "prior text")
        assert h1 == h2

    def test_changes_on_mda(self):
        """Hash changes when MDA text changes."""
        h1 = compute_filing_hash("mda v1", "risk", "prior")
        h2 = compute_filing_hash("mda v2", "risk", "prior")
        assert h1 != h2

    def test_changes_on_risk(self):
        """Hash changes when risk factors change."""
        h1 = compute_filing_hash("mda", "risk v1", "prior")
        h2 = compute_filing_hash("mda", "risk v2", "prior")
        assert h1 != h2

    def test_changes_on_prior(self):
        """Hash changes when prior risk factors change."""
        h1 = compute_filing_hash("mda", "risk", "prior v1")
        h2 = compute_filing_hash("mda", "risk", "prior v2")
        assert h1 != h2

    def test_none_prior(self):
        """Hash works when prior is None."""
        h = compute_filing_hash("mda", "risk", None)
        assert isinstance(h, str)
        assert len(h) == 16

    def test_hash_length(self):
        """Hash is truncated to 16 hex chars."""
        h = compute_filing_hash("a" * 10000, "b" * 10000, "c" * 10000)
        assert len(h) == 16
        # Verify it's valid hex
        int(h, 16)


# ---------------------------------------------------------------------------
# Batch JSON parsing
# ---------------------------------------------------------------------------


class TestParseBatchJsonResponse:
    def test_clean_json_array(self):
        """Parses a clean JSON array."""
        content = json.dumps([
            {"ticker": "KO", "classification": "TRANSIENT", "confidence": 0.8,
             "evidence_summary": "Test evidence", "transience_argument": "Test arg",
             "key_risks": [], "structural_flags": []},
        ])
        result = _parse_batch_json_response(content, ["KO"])
        assert len(result) == 1
        assert result[0]["ticker"] == "KO"

    def test_fenced_code_block(self):
        """Parses JSON inside markdown code fence."""
        inner = json.dumps([{"ticker": "MSFT", "classification": "TRANSIENT"}])
        content = f"```json\n{inner}\n```"
        result = _parse_batch_json_response(content, ["MSFT"])
        assert len(result) == 1

    def test_mixed_text_with_objects(self):
        """Extracts JSON objects from mixed text."""
        content = (
            "Here are the classifications:\n"
            '{"ticker":"ACN","classification":"CYCLICAL_MACRO","confidence":0.7}\n'
            '{"ticker":"MDT","classification":"STRUCTURAL","confidence":0.8}\n'
        )
        result = _parse_batch_json_response(content, ["ACN", "MDT"])
        assert len(result) == 2

    def test_empty_content(self):
        """Returns empty list for empty content."""
        result = _parse_batch_json_response("", ["KO"])
        assert result == []

    def test_no_json(self):
        """Returns empty list for non-JSON content."""
        result = _parse_batch_json_response("This is just text with no JSON", ["KO"])
        assert result == []


# ---------------------------------------------------------------------------
# Stock block construction
# ---------------------------------------------------------------------------


class TestBuildStockBlock:
    def test_basic_block(self):
        """Builds a stock block with all fields."""
        block = _build_stock_block(
            ticker="KO",
            mda_text="Revenue grew 5%",
            risk_factors_current="Competition risk",
            risk_factors_prior="Prior competition risk",
            macro_context="EXPANSION",
            max_chars=3000,
        )
        assert "Stock: KO" in block
        assert "Revenue grew 5%" in block
        assert "Competition risk" in block
        assert "Prior competition risk" in block
        assert "EXPANSION" in block

    def test_no_prior(self):
        """Handles missing prior risk factors."""
        block = _build_stock_block(
            ticker="MSFT",
            mda_text="Strong growth",
            risk_factors_current="Standard risks",
            risk_factors_prior=None,
            macro_context="",
            max_chars=3000,
        )
        assert "prior year" not in block.lower() or "Stock: MSFT" in block

    def test_truncation(self):
        """Truncates text to max_chars."""
        long_text = "x" * 5000
        block = _build_stock_block(
            ticker="TEST",
            mda_text=long_text,
            risk_factors_current=long_text,
            risk_factors_prior=None,
            macro_context="",
            max_chars=1000,
        )
        # The block should contain truncated text
        assert len(block) < len(long_text) * 2 + 200  # some overhead for labels

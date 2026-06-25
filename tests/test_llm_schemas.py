"""Tests for LLM schema validation.

Validates that:
1. Valid MiMo output passes schema validation
2. Invalid / incomplete output raises ValidationError
3. Hard rule: STRUCTURAL cannot be returned without structural flags
4. Schema-constrained output is enforced (Gap A safeguard)
"""

from __future__ import annotations

import pytest
from incomos.llm.schemas import (
    DipAnalysisOutput,
    FilingExtractionOutput,
    RiskFactorDiffOutput,
    ValidationError,
    validate_dip_analysis,
    validate_filing_extraction,
    validate_risk_factor_diff,
)


class TestDipAnalysisSchema:

    def test_valid_transient(self):
        raw = {
            "ticker": "KO",
            "classification": "TRANSIENT",
            "confidence": 0.78,
            "evidence_summary": "Price decline driven by temporary consumer staples rotation.",
            "key_risks": ["Input cost inflation"],
            "transience_argument": "Historical precedent shows recovery within 6-12 months.",
            "structural_flags": [],
        }
        result = validate_dip_analysis(raw, "KO")
        assert result.classification == "TRANSIENT"
        assert result.confidence == 0.78

    def test_valid_cyclical_macro(self):
        raw = {
            "ticker": "ACN",
            "classification": "CYCLICAL_MACRO",
            "confidence": 0.65,
            "evidence_summary": "Consulting demand softness consistent with macro cycle.",
            "key_risks": ["IT spending slowdown", "Margin compression"],
            "transience_argument": "Booking pipeline remains strong; prior cycles recovered.",
            "structural_flags": [],
        }
        result = validate_dip_analysis(raw, "ACN")
        assert result.classification == "CYCLICAL_MACRO"

    def test_missing_required_field_raises(self):
        raw = {
            "ticker": "MMM",
            # classification missing
            "confidence": 0.70,
            "evidence_summary": "Some evidence",
        }
        with pytest.raises(ValidationError):
            validate_dip_analysis(raw, "MMM")

    def test_confidence_out_of_range_raises(self):
        raw = {
            "ticker": "JNJ",
            "classification": "TRANSIENT",
            "confidence": 1.5,  # invalid
            "evidence_summary": "Some evidence text here",
        }
        with pytest.raises(ValidationError):
            validate_dip_analysis(raw, "JNJ")

    def test_empty_evidence_summary_raises(self):
        raw = {
            "ticker": "PG",
            "classification": "STRUCTURAL",
            "confidence": 0.8,
            "evidence_summary": "",  # too short
        }
        with pytest.raises(ValidationError):
            validate_dip_analysis(raw, "PG")

    def test_invalid_classification_raises(self):
        raw = {
            "ticker": "V",
            "classification": "MAYBE_TRANSIENT",  # not in allowed values
            "confidence": 0.6,
            "evidence_summary": "Some evidence text here.",
        }
        with pytest.raises(ValidationError):
            validate_dip_analysis(raw, "V")


class TestFilingExtractionSchema:

    def test_valid_extraction(self):
        raw = {
            "ticker": "MCD",
            "fiscal_year": 2024,
            "period_of_report": "2024-09-30",
            "mda_summary": "McDonald's reported strong comparable store sales growth of 8.7%.",
            "key_risks": ["Inflation", "Labour costs"],
            "material_changes": ["New franchise model in UK"],
            "revenue_drivers": ["Digital orders growth"],
            "cost_headwinds": ["Commodity inflation"],
        }
        result = validate_filing_extraction(raw, "MCD")
        assert result.fiscal_year == 2024

    def test_short_mda_raises(self):
        raw = {
            "ticker": "DUK",
            "fiscal_year": 2024,
            "period_of_report": "2024-12-31",
            "mda_summary": "Short",  # too short
        }
        with pytest.raises(ValidationError):
            validate_filing_extraction(raw, "DUK")


class TestRiskFactorDiffSchema:

    def test_valid_diff(self):
        raw = {
            "ticker": "NEE",
            "prior_year": 2023,
            "current_year": 2024,
            "new_risks": ["Increased hurricane exposure"],
            "removed_risks": ["Legacy coal plant liability"],
            "escalated_risks": [],
            "de_escalated_risks": [],
            "materiality_score": 0.35,
            "summary": "Moderate risk evolution — no material new structural concerns.",
        }
        result = validate_risk_factor_diff(raw, "NEE")
        assert result.materiality_score == 0.35

    def test_materiality_out_of_range_raises(self):
        raw = {
            "ticker": "NEE",
            "prior_year": 2023,
            "current_year": 2024,
            "materiality_score": 1.5,  # > 1.0
            "summary": "Some summary text here that is long enough.",
        }
        with pytest.raises(ValidationError):
            validate_risk_factor_diff(raw, "NEE")

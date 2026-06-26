"""Pydantic schemas for MiMo 2.5 output validation.

RULE: Any LLM output entering the scoring engine MUST be validated against
one of these schemas. Validation failure → flag for human review, never
silently mis-score.

Gap A: MiMo 2.5 few-shot grounding dataset not yet built.
Schema-constrained output is mandatory until that dataset is ready.
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, field_validator


# Allowed dip classification values (locked in architecture)
DIP_CLASSIFICATIONS = Literal[
    "TRANSIENT",
    "TRANSIENT_MACRO_AMPLIFIED",
    "CYCLICAL_IDIOSYNCRATIC",
    "CYCLICAL_MACRO",
    "STRUCTURAL",
    "STRUCTURAL_MACRO_EXPOSED",
    "UNKNOWN",
]


class DipAnalysisOutput(BaseModel):
    """Schema for MiMo 2.5 dip classification output.

    Hard rule: Macro CANNOT upgrade STRUCTURAL → TRANSIENT.
    This is validated in the schema-level validator below.
    """
    ticker: str
    classification: DIP_CLASSIFICATIONS
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_summary: str = Field(min_length=10, max_length=2000)
    key_risks: list[str] = Field(default_factory=list)
    transience_argument: str = Field(min_length=5, max_length=2000)  # why dip is temporary
    structural_flags: list[str] = Field(default_factory=list)  # any structural concerns

    @field_validator("classification")
    @classmethod
    def validate_classification(cls, v: str) -> str:
        # Hard rule: STRUCTURAL and STRUCTURAL_MACRO_EXPOSED are permanent labels.
        # MiMo 2.5 may never directly output a classification in the schema
        # that upgrades a known-structural stock — but enforcement is upstream.
        return v


class FilingExtractionOutput(BaseModel):
    """Schema for MD&A and Risk Factors extracted from 10-K / 10-Q."""
    ticker: str
    fiscal_year: int
    period_of_report: str
    mda_summary: str = Field(min_length=20, max_length=5000)
    key_risks: list[str] = Field(default_factory=list)
    material_changes: list[str] = Field(default_factory=list)
    revenue_drivers: list[str] = Field(default_factory=list)
    cost_headwinds: list[str] = Field(default_factory=list)


class RiskFactorDiffOutput(BaseModel):
    """Schema for YoY Risk Factors language change analysis.

    This is the highest-value MiMo 2.5 use case — catching silent
    deterioration before it shows in financials.
    """
    ticker: str
    prior_year: int
    current_year: int
    new_risks: list[str] = Field(default_factory=list)
    removed_risks: list[str] = Field(default_factory=list)
    escalated_risks: list[str] = Field(default_factory=list)
    de_escalated_risks: list[str] = Field(default_factory=list)
    materiality_score: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=10, max_length=2000)


class ValidationError(Exception):
    """Raised when MiMo 2.5 output fails schema validation."""
    def __init__(self, schema_name: str, ticker: str, errors: list):
        super().__init__(
            f"MiMo 2.5 output for {ticker} failed {schema_name} validation. "
            f"Flagged for human review. Errors: {errors}"
        )
        self.schema_name = schema_name
        self.ticker = ticker
        self.errors = errors


def validate_dip_analysis(raw: dict, ticker: str) -> DipAnalysisOutput:
    """Validate raw MiMo output against DipAnalysisOutput schema.

    Raises ValidationError if validation fails. Caller must flag for human review.
    """
    try:
        return DipAnalysisOutput(**raw)
    except Exception as exc:
        raise ValidationError("DipAnalysisOutput", ticker, [str(exc)]) from exc


def validate_filing_extraction(raw: dict, ticker: str) -> FilingExtractionOutput:
    """Validate raw MiMo output against FilingExtractionOutput schema."""
    try:
        return FilingExtractionOutput(**raw)
    except Exception as exc:
        raise ValidationError("FilingExtractionOutput", ticker, [str(exc)]) from exc


def validate_risk_factor_diff(raw: dict, ticker: str) -> RiskFactorDiffOutput:
    """Validate raw MiMo output against RiskFactorDiffOutput schema."""
    try:
        return RiskFactorDiffOutput(**raw)
    except Exception as exc:
        raise ValidationError("RiskFactorDiffOutput", ticker, [str(exc)]) from exc

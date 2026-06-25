"""MiMo 2.5 API client.

Raises MimoNotConfiguredError if MIMO_API_KEY is not set.
All responses are validated against Pydantic schemas before return.
Validation failure raises ValidationError — caller flags for human review.

LLM Strategy Rule (architecture locked):
  Rule-based MCP tools FIRST. MiMo 2.5 ONLY when rule-based fails or returns null.
  This module is never called from Stage 0-1 or Stage 1-2.
  It is only called from Stage 2-3 (context check) and Stage 3-4 (full DD).

Gap A: MiMo 2.5 few-shot grounding dataset not yet built.
Schema-constrained output is the primary guard until that dataset is ready.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from incomos.core.config import get_settings
from incomos.llm.schemas import (
    DipAnalysisOutput,
    FilingExtractionOutput,
    RiskFactorDiffOutput,
    ValidationError,
    validate_dip_analysis,
    validate_filing_extraction,
    validate_risk_factor_diff,
)

logger = logging.getLogger(__name__)


def _assert_configured() -> None:
    """Raise MimoNotConfiguredError if MIMO_API_KEY is absent."""
    from incomos.core.exceptions import MimoNotConfiguredError
    if not get_settings().mimo_api_key:
        raise MimoNotConfiguredError()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _call_api(endpoint: str, payload: dict[str, Any]) -> dict:
    """Make an authenticated API call to MiMo 2.5."""
    settings = get_settings()
    base_url = settings.mimo_api_base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {settings.mimo_api_key}",
        "Content-Type": "application/json",
    }
    # No max_tokens — let model use its full context window (256k)
    with httpx.Client(timeout=120) as client:
        resp = client.post(f"{base_url}/{endpoint}", json=payload, headers=headers)
        resp.raise_for_status()
    return resp.json()


def analyze_dip(
    ticker: str,
    mda_text: str,
    risk_factors_current: str,
    risk_factors_prior: str | None = None,
    macro_context: str = "",
) -> DipAnalysisOutput:
    """Request dip classification from MiMo 2.5.

    Raises MimoNotConfiguredError if MIMO_API_KEY is not set.
    Raises schemas.ValidationError if output fails schema validation.
    """
    _assert_configured()

    prompt = (
        f"Stock: {ticker}\n"
        f"Macro context: {macro_context}\n\n"
        f"=== MD&A ===\n{mda_text[:3000]}\n\n"
        f"=== Current Risk Factors ===\n{risk_factors_current[:3000]}\n\n"
    )
    if risk_factors_prior:
        prompt += f"=== Prior Year Risk Factors (for diff) ===\n{risk_factors_prior[:3000]}\n\n"

    prompt += (
        "Classify the nature of this stock's current price decline. "
        "Return JSON matching this schema: "
        '{"ticker":"...","classification":"TRANSIENT|TRANSIENT_MACRO_AMPLIFIED|'
        'CYCLICAL_IDIOSYNCRATIC|CYCLICAL_MACRO|STRUCTURAL|STRUCTURAL_MACRO_EXPOSED|UNKNOWN",'
        '"confidence":0.0-1.0,"evidence_summary":"...","key_risks":[],'
        '"transience_argument":"...","structural_flags":[]}'
    )

    raw = _call_api("chat/completions", {
        "model": get_settings().mimo_model,
        "thinking": True,
        "messages": [{"role": "user", "content": prompt}],
    })

    # Extract content from response
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    # Parse JSON from response — MiMo 2.5 should return structured output
    import json
    try:
        result_dict = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValidationError("DipAnalysisOutput", ticker, [f"Response is not valid JSON: {exc}"]) from exc

    # MANDATORY schema validation before returning
    return validate_dip_analysis(result_dict, ticker)


def extract_filing_data(
    ticker: str,
    filing_text: str,
    fiscal_year: int,
    period_of_report: str,
) -> FilingExtractionOutput:
    """Extract structured data from a 10-K or 10-Q filing using MiMo 2.5.

    Raises MimoNotConfiguredError if MIMO_API_KEY is not set.
    """
    _assert_configured()

    raw = _call_api("chat/completions", {
        "model": get_settings().mimo_model,
        "thinking": True,
        "messages": [{
            "role": "user",
            "content": (
                f"Stock: {ticker}, FY: {fiscal_year}, Period: {period_of_report}\n\n"
                f"{filing_text[:8000]}\n\n"
                "Extract structured data from this SEC filing. Return JSON matching: "
                '{"ticker":"...","fiscal_year":N,"period_of_report":"...","mda_summary":"...",'
                '"key_risks":[],"material_changes":[],"revenue_drivers":[],"cost_headwinds":[]}'
            ),
        }],
    })

    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    import json
    try:
        result_dict = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValidationError("FilingExtractionOutput", ticker, [f"Response is not valid JSON: {exc}"]) from exc

    return validate_filing_extraction(result_dict, ticker)

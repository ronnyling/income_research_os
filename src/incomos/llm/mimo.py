"""MiMo 2.5 API client.

Raises MimoNotConfiguredError if MIMO_API_KEY is not set.
All responses are validated against Pydantic schemas before return.
Validation failure raises ValidationError — caller flags for human review.

LLM Strategy Rule (architecture locked):
    Rule-based MCP tools FIRST. MiMo 2.5 ONLY when rule-based fails or returns null.
    This module is never called from Stage 0-1 or Stage 1-2.
    It is only called from Stage 2-3 (context check) and Stage 3-4 (full DD).

Few-shot grounding examples are embedded directly in the dip-classification prompt.
Schema-constrained output remains mandatory before any score uses MiMo output.
"""

from __future__ import annotations

import re
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


def _parse_json_response(content: str, schema_name: str, ticker: str) -> dict:
    """Parse a MiMo response that should contain one JSON object."""
    import json

    stripped = content.strip()
    if not stripped:
        raise ValidationError(schema_name, ticker, ["Response content was empty"])

    candidates: list[str] = [stripped]

    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())

    first_brace = stripped.find("{")
    if first_brace != -1:
        candidates.append(stripped[first_brace:])

    decoder = json.JSONDecoder()
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            result_dict, _ = decoder.raw_decode(candidate)
            if isinstance(result_dict, dict):
                return result_dict
            last_error = ValidationError(schema_name, ticker, ["Response JSON was not an object"])
        except Exception as exc:
            last_error = exc

    raise ValidationError(schema_name, ticker, [f"Response is not valid JSON: {last_error}"]) from last_error


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
        "### Few-shot examples (do NOT copy these; classify the stock above) ###\n\n"
        "Example 1 — TRANSIENT\n"
        "Stock: DEF  Macro: Rates rising moderately\n"
        "MD&A: Revenue grew 8% YoY driven by new product lines.  One-time legal settlement of $120M "
        "depressed net income this quarter.  Core operating margins expanded 60 bps.\n"
        "Risk factors (current vs prior): Unchanged — litigation risk removed after settlement.\n"
        'Expected output: {"ticker":"DEF","classification":"TRANSIENT","confidence":0.82,'
        '"evidence_summary":"One-time legal charge caused the dip; underlying business is healthy.",'
        '"key_risks":["none material"],"transience_argument":"Settlement removes the overhang; '
        'earnings will normalise next quarter.","structural_flags":[]}\n\n'

        "Example 2 — STRUCTURAL\n"
        "Stock: GHI  Macro: Rates elevated\n"
        "MD&A: Revenue declined 18% YoY as key product line faces permanent commoditisation.  "
        "Gross margin compressed from 52% to 31%.  Management is unable to identify a path back "
        "to prior margin levels.  Customer churn accelerated.\n"
        "Risk factors (current vs prior): New risk added: 'Inability to compete with lower-cost '  "
        "'alternatives may permanently impair our business model.'\n"
        'Expected output: {"ticker":"GHI","classification":"STRUCTURAL","confidence":0.91,'
        '"evidence_summary":"Permanent commoditisation and margin collapse indicate structural impairment.",'
        '"key_risks":["commoditisation","customer churn"],'
        '"transience_argument":"No credible path to margin recovery described.",'
        '"structural_flags":["margin collapse","commoditisation risk added"]}\n\n'

        "Example 3 — CYCLICAL_IDIOSYNCRATIC\n"
        "Stock: JKL  Macro: Mild slowdown\n"
        "MD&A: Revenue down 12% due to inventory de-stocking by major retailers following a "
        "demand spike in the prior year.  Long-term demand for the product category is intact.  "
        "Management expects normalisation within 2-3 quarters.\n"
        "Risk factors (current vs prior): No new structural risks added.\n"
        'Expected output: {"ticker":"JKL","classification":"CYCLICAL_IDIOSYNCRATIC","confidence":0.79,'
        '"evidence_summary":"De-stocking cycle is the driver; category demand intact.",'
        '"key_risks":["re-stocking timing uncertain"],'
        '"transience_argument":"Inventory cycles typically resolve in 2-3 quarters; no structural damage.",'
        '"structural_flags":[]}\n\n'
        "### End of examples ###\n\n"

        "Classify the nature of this stock's current price decline. "
        "Return JSON matching this schema: "
        '{"ticker":"...","classification":"TRANSIENT|TRANSIENT_MACRO_AMPLIFIED|'
        'CYCLICAL_IDIOSYNCRATIC|CYCLICAL_MACRO|STRUCTURAL|STRUCTURAL_MACRO_EXPOSED|UNKNOWN",'
        '"confidence":0.0-1.0,"evidence_summary":"...","key_risks":[],'
        '"transience_argument":"...","structural_flags":[]}'
        "\n\nReturn only the JSON object. No markdown, no explanation, no code fences."
    )

    raw = _call_api("chat/completions", {
        "model": get_settings().mimo_model,
        "thinking": True,
        "messages": [{"role": "user", "content": prompt}],
    })

    # Extract content from response
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    # Parse JSON from response — MiMo 2.5 should return structured output.
    result_dict = _parse_json_response(content, "DipAnalysisOutput", ticker)

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
    result_dict = _parse_json_response(content, "FilingExtractionOutput", ticker)

    return validate_filing_extraction(result_dict, ticker)

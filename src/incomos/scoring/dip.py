"""Dip Quality Score — requires validated MiMo 2.5 analysis.

Base scores and flag penalties are read from config (no hardcoded values).
Raises MimoAnalysisRequiredError if called without a validated MiMo result.

Score formula (all thresholds from config):
  confidence_adjusted = 50 + (base - 50) * confidence
  final = confidence_adjusted − (flag_count × flag_penalty), capped at flag_penalty_max
"""

from __future__ import annotations

import logging

from incomos.core.config import get_settings
from incomos.core.exceptions import MimoAnalysisRequiredError
from incomos.core.types import DipClassification

logger = logging.getLogger(__name__)


def compute_dip_quality(
    ticker: str,
    mimo_result: dict | None = None,
) -> tuple[float, dict]:
    """Return (score 0-100, breakdown dict).

    mimo_result MUST be provided — it must have already been validated against
    DipAnalysisOutput schema (see llm/schemas.py) before being passed here.
    Raises MimoAnalysisRequiredError if mimo_result is None.
    """
    if mimo_result is None:
        raise MimoAnalysisRequiredError(ticker)

    cfg = get_settings().dip_q

    base_scores = {
        "TRANSIENT": cfg.base_transient,
        "TRANSIENT_MACRO_AMPLIFIED": cfg.base_transient_macro,
        "CYCLICAL_MACRO": cfg.base_cyclical_macro,
        "CYCLICAL_IDIOSYNCRATIC": cfg.base_cyclical_idio,
        "STRUCTURAL_MACRO_EXPOSED": cfg.base_structural_exposed,
        "STRUCTURAL": cfg.base_structural,
        "UNKNOWN": cfg.base_unknown,
    }

    classification = mimo_result.get("classification", "UNKNOWN")
    confidence = mimo_result.get("confidence", 0.5)
    evidence = mimo_result.get("evidence_summary", "")
    structural_flags = mimo_result.get("structural_flags", []) or []

    base = base_scores.get(classification, cfg.base_unknown)
    # Confidence-adjust: confidence=0 → neutral 50; confidence=1 → full base score.
    confidence_adjusted = 50 + (base - 50) * confidence

    flag_count = len(structural_flags)
    flag_penalty = min(flag_count * cfg.flag_penalty, cfg.flag_penalty_max)
    final = round(min(100, max(0, confidence_adjusted - flag_penalty)), 1)

    return final, {
        "classification": classification,
        "confidence": confidence,
        "score": final,
        "is_stub": False,
        "evidence_summary": evidence,
        "structural_flag_count": flag_count,
        "flag_penalty": flag_penalty,
    }


from __future__ import annotations

import logging


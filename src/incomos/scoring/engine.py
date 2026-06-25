"""Scoring engine — orchestrates all 4 dimensions into an OpportunityScore.

All weights and multiplier thresholds are read from config.
No mock / stub behavior: both price_snap and mimo_dip_result are required.
Raises if either is missing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from incomos.core.config import get_settings
from incomos.core.exceptions import MimoAnalysisRequiredError, PriceDataUnavailableError
from incomos.data.market import PriceSnapshot
from incomos.core.types import XBRLMetrics
from incomos.scoring.income import compute_income_quality
from incomos.scoring.business import compute_business_quality
from incomos.scoring.oversold import compute_oversold_confidence
from incomos.scoring.dip import compute_dip_quality

logger = logging.getLogger(__name__)


@dataclass
class ScoringResult:
    ticker: str
    income_quality: float
    income_breakdown: dict
    business_quality: float
    business_breakdown: dict
    dip_quality: float
    dip_breakdown: dict
    oversold_confidence: float
    oversold_breakdown: dict
    composite: float
    base_size_multiplier: float
    is_partial: bool
    partial_reasons: list[str]


def score(
    ticker: str,
    metrics: list[XBRLMetrics],
    price_snap: PriceSnapshot,
    mimo_dip_result: dict,
) -> ScoringResult:
    """Compute the full OpportunityScore for a stock.

    Both price_snap and mimo_dip_result are required.
    Raises PriceDataUnavailableError if snap quality is FAILED.
    Raises MimoAnalysisRequiredError if mimo_dip_result is not provided.
    All weights and thresholds are read from config.
    """
    cfg = get_settings()
    w = cfg.scoring_weights
    m = cfg.score_multiplier

    iq_score, iq_breakdown = compute_income_quality(metrics)
    bq_score, bq_breakdown = compute_business_quality(metrics)
    dq_score, dq_breakdown = compute_dip_quality(ticker, mimo_dip_result)
    oc_score, oc_breakdown = compute_oversold_confidence(price_snap)

    composite = round(
        w.income * iq_score
        + w.business * bq_score
        + w.dip * dq_score
        + w.oversold * oc_score,
        1,
    )

    if composite >= m.high_threshold:
        multiplier = m.high
    elif composite >= m.mid_threshold:
        multiplier = m.mid
    else:
        multiplier = m.low

    logger.info(
        "%s | IQ=%.1f BQ=%.1f DQ=%.1f OC=%.1f → composite=%.1f (×%.1f)",
        ticker, iq_score, bq_score, dq_score, oc_score, composite, multiplier,
    )

    return ScoringResult(
        ticker=ticker,
        income_quality=iq_score,
        income_breakdown=iq_breakdown,
        business_quality=bq_score,
        business_breakdown=bq_breakdown,
        dip_quality=dq_score,
        dip_breakdown=dq_breakdown,
        oversold_confidence=oc_score,
        oversold_breakdown=oc_breakdown,
        composite=composite,
        base_size_multiplier=multiplier,
        is_partial=False,
        partial_reasons=[],
    )

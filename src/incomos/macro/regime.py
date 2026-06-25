"""Macro Regime Detector — 4-axis, confidence-gated.

Implements the MacroRegimeContract (v1 states only):
  Market structure: TRENDING_UP | TRENDING_DOWN | RANGING
  Growth:          EXPANSION | RECESSION_RISK
  Rates:           STABLE | RATE_SHOCK_UP | RATE_SHOCK_DOWN
  Financial cond.: LOOSE | TIGHTENING

Data sources (all free):
  Market structure — yfinance S&P 500 price trend
  Growth           — FRED RECPROUSM156N (NY Fed recession probability, monthly)
  Rates            — FRED DGS2 velocity (5d/20d change in basis points)
  Fin. conditions  — FRED NFCI / ANFCI (Chicago Fed, weekly)

Hard rules enforced here:
  - Macro CANNOT upgrade STRUCTURAL → TRANSIENT (enforced in reconcile module)
  - Confidence < 0.55 → reference only
  - Confidence 0.55–0.70 → adjust weights
  - Confidence ≥ 0.85 + sector_override_eligible → override eligible
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from incomos.core.config import get_settings
from incomos.core.exceptions import MacroDataUnavailableError, PriceDataUnavailableError
from incomos.core.types import (
    FinancialConditions,
    GrowthState,
    MacroAxisReading,
    MacroRegimeContract,
    MacroUsagePolicy,
    MarketStructure,
    RatesState,
    SectorImpact,
)
from incomos.data.fred import get_latest_value, get_recent_series
from incomos.data.market import get_price_snapshot

logger = logging.getLogger(__name__)


def _detect_market_structure(ticker: str = "^GSPC") -> MacroAxisReading:
    """Derive market structure from S&P 500 price trend.

    Raises PriceDataUnavailableError if yfinance fetch fails.
    """
    snap = get_price_snapshot(ticker)  # raises PriceDataUnavailableError on failure
    trend = snap.price_trend
    conf = get_settings().macro.growth_confidence if snap.data_quality == "GOOD" else 0.50
    sev = snap.pct_below_52w_high
    return MacroAxisReading(state=trend, confidence=conf, severity=min(sev, 1.0))


def _detect_growth_state() -> MacroAxisReading:
    """Use FRED NY Fed recession probability (RECPROUSM156N) for growth regime.

    Raises MacroDataUnavailableError if FRED fetch fails.
    """
    cfg = get_settings().macro
    prob = get_latest_value("RECPROUSM156N", lookback_days=60)  # raises on failure
    # prob is a percentage (e.g., 17.3 means 17.3%)
    prob_frac = prob / 100.0
    if prob_frac >= cfg.recession_prob_threshold:
        state = GrowthState.RECESSION_RISK
        severity = min(prob_frac / 0.50, 1.0)
    else:
        state = GrowthState.EXPANSION
        severity = prob_frac / cfg.recession_prob_threshold
    conf = cfg.growth_confidence  # NY Fed model is well-calibrated for medium-term
    logger.info("NY Fed recession prob: %.1f%% → %s", prob, state)
    return MacroAxisReading(state=state, confidence=conf, severity=severity)


def _detect_rates_state() -> MacroAxisReading:
    """Detect rate shock from FRED DGS2 velocity (5-day change in bps).

    Raises MacroDataUnavailableError if FRED fetch fails or insufficient data.
    """
    cfg = get_settings().macro
    series = get_recent_series("DGS2", n=10)  # raises on failure
    if len(series) < 6:
        raise MacroDataUnavailableError("DGS2", f"only {len(series)} observations, need >=6")
    recent_5 = [v for _, v in series[-5:]]
    prev_1 = series[-6][1]
    latest = recent_5[-1]
    change_bps = (latest - prev_1) * 100  # convert % to bps
    if change_bps >= cfg.rate_shock_up_bps:
        state = RatesState.RATE_SHOCK_UP
        severity = min(change_bps / 50, 1.0)
        conf = 0.80
    elif change_bps <= cfg.rate_shock_down_bps:
        state = RatesState.RATE_SHOCK_DOWN
        severity = min(abs(change_bps) / 50, 1.0)
        conf = 0.80
    else:
        state = RatesState.STABLE
        severity = abs(change_bps) / cfg.rate_shock_up_bps
        conf = 0.85
    logger.info("DGS2 5d change: %.1f bps → %s", change_bps, state)
    return MacroAxisReading(state=state, confidence=conf, severity=severity)


def _detect_financial_conditions() -> MacroAxisReading:
    """Use FRED NFCI for financial conditions axis.

    Raises MacroDataUnavailableError if FRED fetch fails.
    """
    cfg = get_settings().macro
    nfci = get_latest_value("NFCI", lookback_days=14)  # raises on failure
    if nfci > cfg.nfci_tightening:
        state = FinancialConditions.TIGHTENING
        severity = min(nfci / 1.0, 1.0)
        conf = cfg.growth_confidence
    elif nfci < cfg.nfci_loose:
        state = FinancialConditions.LOOSE
        severity = min(abs(nfci) / 0.5, 1.0)
        conf = cfg.growth_confidence
    else:
        state = FinancialConditions.LOOSE  # neutral treated as loose for income stocks
        severity = abs(nfci) / cfg.nfci_tightening
        conf = 0.65
    logger.info("NFCI: %.4f → %s", nfci, state)
    return MacroAxisReading(state=state, confidence=conf, severity=severity)


def detect_macro_regime(sector: str = "General", ticker: str | None = None) -> MacroRegimeContract:
    """Build the full MacroRegimeContract from live data sources.

    Called by: staleness-triggered refresh on startup (data_type = 'macro_regime').
    TTL: 24 hours.
    """
    logger.info("Building macro regime contract...")

    market = _detect_market_structure()
    growth = _detect_growth_state()
    rates = _detect_rates_state()
    fin_cond = _detect_financial_conditions()

    # Derive primary regime (highest confidence + highest severity)
    axes = {
        "market_structure": market,
        "growth": growth,
        "rates": rates,
        "financial_conditions": fin_cond,
    }
    primary_axis = max(axes.items(), key=lambda kv: kv[1].confidence * kv[1].severity)
    primary_regime = primary_axis[1].state
    primary_confidence = primary_axis[1].confidence

    # Sector impact (V1 defaults — MY stocks always sector_override_eligible=False)
    sector_impact = SectorImpact(
        sector=sector,
        macro_alignment=0.40,
        peer_drawdown_breadth=0.30,
        sector_override_eligible=False,   # V1: conservative default
    )

    contract = MacroRegimeContract(
        as_of=datetime.now(timezone.utc),
        primary_regime=str(primary_regime),
        primary_confidence=round(primary_confidence, 3),
        market_structure=market,
        growth=growth,
        rates=rates,
        financial_conditions=fin_cond,
        sector_impact=sector_impact,
        usage_policy=MacroUsagePolicy(),
        evidence=[
            {"name": "market_structure", "state": str(market.state), "confidence": market.confidence},
            {"name": "ny_fed_recession_prob", "state": str(growth.state), "confidence": growth.confidence},
            {"name": "dgs2_velocity", "state": str(rates.state), "confidence": rates.confidence},
            {"name": "nfci", "state": str(fin_cond.state), "confidence": fin_cond.confidence},
        ],
    )
    logger.info(
        "Macro regime: primary=%s confidence=%.2f | structure=%s growth=%s rates=%s fin=%s",
        primary_regime, primary_confidence,
        market.state, growth.state, rates.state, fin_cond.state,
    )
    return contract

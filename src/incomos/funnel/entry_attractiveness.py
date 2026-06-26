"""Entry Attractiveness Score — continuous ranking for Stage 1→2.

Replaces the binary dip trigger with a multi-dimensional attractiveness
score (0-100) that identifies the best entry opportunities across ALL
quality-screened prospects, not just those crossing an arbitrary dip threshold.

Components (all configurable via EntryAttractivenessCfg):
  1. Price drawdown (0-30)    — deeper dip = higher score
  2. Yield expansion (0-25)   — current yield vs 5yr average
  3. Dividend growth (0-20)   — DPS CAGR trajectory
  4. Valuation signal (0-15)  — earnings yield / value signal
  5. Trend/momentum (0-10)    — TRENDING_DOWN bonus

Each stock is tagged with the criteria it scored highest on:
  DEEP_DIP, YIELD_EXPANSION, DIVIDEND_GROWTH, UNDERVALUED, TRENDING_DOWN

No LLM calls. Cost remains near-zero. All thresholds from config.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from incomos.core.config import get_settings
from incomos.data.market import PriceSnapshot
from incomos.core.types import XBRLMetrics

logger = logging.getLogger(__name__)


@dataclass
class EntryAttractivenessResult:
    """Result of the entry attractiveness evaluation for one stock."""
    ticker: str
    score: float                        # 0-100 continuous
    tags: list[str]                     # Criteria tags (e.g. DEEP_DIP, YIELD_EXPANSION)
    component_scores: dict[str, float]  # Per-component raw scores
    meets_floor: bool                   # Whether score >= min_attractiveness_score
    data_completeness: str              # FULL | PARTIAL | MINIMAL


def _score_drawdown(pct_below_52w_high: float, cfg: object | None = None) -> tuple[float, str | None]:
    """Score price drawdown from 52W high. Max 100 pts (scaled by config weight).

    Breakpoints are configurable via EntryAttractivenessCfg.
    Returns (raw_score_0_100, tag_or_None).
    """
    pct = pct_below_52w_high
    # Use config breakpoints if available, else hardcoded defaults
    t1 = getattr(cfg, 'drawdown_t1', 0.02)
    t2 = getattr(cfg, 'drawdown_t2', 0.05)
    t3 = getattr(cfg, 'drawdown_t3', 0.10)
    t4 = getattr(cfg, 'drawdown_t4', 0.15)
    t5 = getattr(cfg, 'drawdown_t5', 0.20)
    t6 = getattr(cfg, 'drawdown_t6', 0.30)
    t7 = getattr(cfg, 'drawdown_t7', 0.40)

    if pct >= t7:
        score = 100.0
    elif pct >= t6:
        score = 83.0 + (pct - t6) / (t7 - t6) * 17.0
    elif pct >= t5:
        score = 67.0 + (pct - t5) / (t6 - t5) * 16.0
    elif pct >= t4:
        score = 50.0 + (pct - t4) / (t5 - t4) * 17.0
    elif pct >= t3:
        score = 33.0 + (pct - t3) / (t4 - t3) * 17.0
    elif pct >= t2:
        score = 17.0 + (pct - t2) / (t3 - t2) * 16.0
    elif pct >= t1:
        score = (pct - t1) / (t2 - t1) * 17.0
    else:
        score = 0.0

    tag = "DEEP_DIP" if pct >= t4 else ("DIP" if pct >= t2 else None)
    return round(score, 1), tag


def _score_yield_expansion(
    current_yield: float | None,
    avg_yield_5yr: float | None,
    yield_expansion_ratio: float | None,
    min_ratio: float,
) -> tuple[float, str | None]:
    """Score yield expansion (current yield vs historical average). Max 100 pts.

    A stock yielding 3.5% against a 5yr average of 2.8% has a ratio of 1.25 —
    it's 25% above its historical yield, meaning price hasn't kept up with
    dividend growth (or the market has derated it).

    Returns (raw_score_0_100, tag_or_None).
    """
    if current_yield is None or avg_yield_5yr is None or yield_expansion_ratio is None:
        return 0.0, None
    if current_yield <= 0 or avg_yield_5yr <= 0:
        return 0.0, None

    ratio = yield_expansion_ratio
    if ratio < min_ratio:
        # Below minimum expansion — score proportionally
        score = max(0, (ratio - 0.8) / (min_ratio - 0.8) * 30.0) if min_ratio > 0.8 else 0.0
        return round(score, 1), None

    # Above minimum expansion — scale up
    # ratio 1.10 = 30pts, 1.20 = 50pts, 1.30 = 70pts, 1.50+ = 100pts
    if ratio >= 1.50:
        score = 100.0
    elif ratio >= 1.30:
        score = 70.0 + (ratio - 1.30) / 0.20 * 30.0
    elif ratio >= 1.20:
        score = 50.0 + (ratio - 1.20) / 0.10 * 20.0
    elif ratio >= 1.10:
        score = 30.0 + (ratio - 1.10) / 0.10 * 20.0
    else:
        score = (ratio - min_ratio) / (1.10 - min_ratio) * 30.0

    tag = "YIELD_EXPANSION" if ratio >= min_ratio else None
    return round(min(100, max(0, score)), 1), tag


def _score_dividend_growth(
    metrics: list[XBRLMetrics],
    min_cagr: float,
) -> tuple[float, str | None]:
    """Score dividend growth trajectory using DPS data from XBRL metrics.

    Computes 3-year DPS CAGR. Higher growth = higher score.
    Returns (raw_score_0_100, tag_or_None).
    """
    if not metrics or len(metrics) < 2:
        return 0.0, None

    # Sort oldest → newest
    sorted_m = sorted(metrics, key=lambda m: m.fiscal_year)

    # Get DPS values (prefer dps_declared, fallback to dps_paid)
    def _dps(m: XBRLMetrics) -> float | None:
        if m.dps_declared is not None and m.dps_declared > 0:
            return m.dps_declared
        if m.dps_paid is not None and m.dps_paid > 0:
            return m.dps_paid
        return None

    # Use latest and ~3 years ago (or earliest available)
    latest = sorted_m[-1]
    earliest = sorted_m[0]

    dps_latest = _dps(latest)
    dps_earliest = _dps(earliest)

    if dps_latest is None or dps_earliest is None or dps_earliest <= 0:
        return 0.0, None

    years = max(1, latest.fiscal_year - earliest.fiscal_year)
    if years < 1:
        return 0.0, None

    cagr = (dps_latest / dps_earliest) ** (1.0 / years) - 1.0

    if cagr < min_cagr:
        return 0.0, None

    # Score: CAGR 0% = 20pts, 3% = 40pts, 5% = 60pts, 8% = 80pts, 12%+ = 100pts
    if cagr >= 0.12:
        score = 100.0
    elif cagr >= 0.08:
        score = 80.0 + (cagr - 0.08) / 0.04 * 20.0
    elif cagr >= 0.05:
        score = 60.0 + (cagr - 0.05) / 0.03 * 20.0
    elif cagr >= 0.03:
        score = 40.0 + (cagr - 0.03) / 0.02 * 20.0
    elif cagr > 0:
        score = 20.0 + cagr / 0.03 * 20.0
    else:
        score = 0.0

    tag = "DIVIDEND_GROWTH" if cagr > 0 else None
    return round(min(100, max(0, score)), 1), tag


def _score_valuation(
    metrics: list[XBRLMetrics],
    current_price: float | None,
    pct_below_52w_high: float,
) -> tuple[float, str | None]:
    """Score valuation using P/E ratio (from XBRL EPS) or FCF margin.

    P/E is the primary signal — it directly measures price vs earnings.
    FCF margin is the fallback when EPS is unavailable.

    Returns (raw_score_0_100, tag_or_None).
    """
    if not metrics or current_price is None or current_price <= 0:
        return 0.0, None

    sorted_m = sorted(metrics, key=lambda m: m.fiscal_year)
    latest = sorted_m[-1]

    # Primary: P/E from XBRL EarningsPerShareBasic
    if latest.earnings_per_share is not None and latest.earnings_per_share > 0:
        pe_ratio = current_price / latest.earnings_per_share
        # Low P/E = cheap = high score
        # P/E < 10 = 90pts, < 15 = 70pts, < 20 = 50pts, < 25 = 30pts, < 35 = 15pts, >= 35 = 0pts
        if pe_ratio < 10:
            score = 90.0
        elif pe_ratio < 15:
            score = 70.0 + (15 - pe_ratio) / 5 * 20.0
        elif pe_ratio < 20:
            score = 50.0 + (20 - pe_ratio) / 5 * 20.0
        elif pe_ratio < 25:
            score = 30.0 + (25 - pe_ratio) / 5 * 20.0
        elif pe_ratio < 35:
            score = (35 - pe_ratio) / 10 * 15.0
        else:
            score = 0.0

        # Bonus: revenue growing + price declining = value opportunity
        if len(sorted_m) >= 2:
            earliest = sorted_m[0]
            if earliest.revenue and latest.revenue and earliest.revenue > 0:
                rev_growth = (latest.revenue - earliest.revenue) / earliest.revenue
                if rev_growth > 0.05 and pct_below_52w_high > 0.05:
                    score = min(100, score + 15)
                elif rev_growth > 0 and pct_below_52w_high > 0.03:
                    score = min(100, score + 5)

        tag = "UNDERVALUED" if score >= 50 else None
        return round(min(100, max(0, score)), 1), tag

    # Fallback: FCF margin as quality signal
    if latest.free_cash_flow is not None and latest.revenue is not None and latest.revenue > 0:
        if latest.net_income is None or latest.net_income <= 0:
            return 0.0, None

        if len(sorted_m) < 2:
            return 0.0, None

        earliest = sorted_m[0]
        if earliest.revenue is None or latest.revenue is None or earliest.revenue <= 0:
            return 0.0, None

        revenue_growth = (latest.revenue - earliest.revenue) / earliest.revenue
        fcf_margin = latest.free_cash_flow / latest.revenue
        if fcf_margin >= 0.25:
            score = 90.0
        elif fcf_margin >= 0.15:
            score = 70.0
        elif fcf_margin >= 0.08:
            score = 50.0
        elif fcf_margin >= 0.03:
            score = 30.0
        elif fcf_margin > 0:
            score = 15.0
        else:
            score = 0.0

        if revenue_growth > 0.05 and pct_below_52w_high > 0.05:
            score = min(100, score + 20)
        elif revenue_growth > 0 and pct_below_52w_high > 0.03:
            score = min(100, score + 10)

        tag = "UNDERVALUED" if score >= 50 else None
        return round(score, 1), tag

    return 0.0, None


def _score_trend(price_trend: str) -> tuple[float, str | None]:
    """Score price trend. TRENDING_DOWN = 100, RANGING = 30, TRENDING_UP = 0.

    Returns (raw_score_0_100, tag_or_None).
    """
    if price_trend == "TRENDING_DOWN":
        return 100.0, "TRENDING_DOWN"
    elif price_trend == "RANGING":
        return 30.0, None
    else:  # TRENDING_UP
        return 0.0, None


def compute_entry_attractiveness(
    ticker: str,
    snap: PriceSnapshot,
    metrics: list[XBRLMetrics],
) -> EntryAttractivenessResult:
    """Compute the entry attractiveness score for a quality-screened stock.

    This is the Stage 1→2 replacement for the binary dip trigger.
    Returns a continuous 0-100 score with component breakdown and tags.

    Raises PriceDataUnavailableError if snap quality is FAILED.
    """
    from incomos.core.exceptions import PriceDataUnavailableError
    if snap.data_quality == "FAILED":
        raise PriceDataUnavailableError(ticker)

    cfg = get_settings().entry_attractiveness

    # Compute each component
    drawdown_score, drawdown_tag = _score_drawdown(snap.pct_below_52w_high, cfg)

    yield_score, yield_tag = _score_yield_expansion(
        snap.current_yield, snap.avg_yield_5yr, snap.yield_expansion_ratio,
        cfg.yield_expansion_min_ratio,
    )

    # Dividend-cut guard: if DPS declined YoY, suppress yield expansion score.
    # A dividend cut shrinks the yield average denominator, creating a false
    # "yield expansion" signal. This guard catches it deterministically from XBRL.
    if yield_score > 0 and metrics and len(metrics) >= 2:
        sorted_m = sorted(metrics, key=lambda m: m.fiscal_year)
        latest_dps = sorted_m[-1].dps_declared or sorted_m[-1].dps_paid
        prior_dps = sorted_m[-2].dps_declared or sorted_m[-2].dps_paid
        if latest_dps is not None and prior_dps is not None and prior_dps > 0:
            dps_change = (latest_dps - prior_dps) / prior_dps
            if dps_change < -0.20:  # DPS dropped >20% — likely dividend cut
                logger.info(
                    "%s: DPS dropped %.1f%% YoY (%.2f -> %.2f) — suppressing yield expansion score",
                    ticker, dps_change * 100, prior_dps, latest_dps,
                )
                yield_score = 0.0
                yield_tag = None

    div_growth_score, div_growth_tag = _score_dividend_growth(
        metrics, cfg.div_growth_min_cagr,
    )

    val_score, val_tag = _score_valuation(metrics, snap.current_price, snap.pct_below_52w_high)

    trend_score, trend_tag = _score_trend(snap.price_trend)

    # Weighted composite
    composite = round(
        cfg.weight_drawdown * drawdown_score
        + cfg.weight_yield_expansion * yield_score
        + cfg.weight_dividend_growth * div_growth_score
        + cfg.weight_valuation * val_score
        + cfg.weight_trend * trend_score,
        1,
    )

    # Clamp to 0-100
    composite = min(100, max(0, composite))

    # Collect tags from all components that scored
    tags: list[str] = []
    for tag in [drawdown_tag, yield_tag, div_growth_tag, val_tag, trend_tag]:
        if tag is not None:
            tags.append(tag)

    # Data completeness
    has_all = all([
        snap.current_yield is not None,
        snap.avg_yield_5yr is not None,
        metrics and len(metrics) >= 2,
    ])
    has_some = any([
        snap.current_yield is not None,
        metrics and len(metrics) >= 1,
    ])
    completeness = "FULL" if has_all else ("PARTIAL" if has_some else "MINIMAL")

    meets_floor = composite >= cfg.min_attractiveness_score

    logger.info(
        "%s | EA=%.1f drawdown=%.0f yield=%.0f divgrowth=%.0f val=%.0f trend=%.0f tags=%s",
        ticker, composite, drawdown_score, yield_score, div_growth_score,
        val_score, trend_score, ",".join(tags) or "none",
    )

    return EntryAttractivenessResult(
        ticker=ticker,
        score=composite,
        tags=tags,
        component_scores={
            "drawdown": drawdown_score,
            "yield_expansion": yield_score,
            "dividend_growth": div_growth_score,
            "valuation": val_score,
            "trend": trend_score,
        },
        meets_floor=meets_floor,
        data_completeness=completeness,
    )

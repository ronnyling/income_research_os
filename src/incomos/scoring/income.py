"""Income Quality Score (0–100).

All scoring tiers read from config (no hardcoded thresholds).
Missing data components score 0 — no partial credit — no benefit of doubt.

Components:
  Dividend continuity   30 pts  (consecutive years paying dividend)
  FCF payout ratio      25 pts  (lower = safer = higher score)
  Dividend growth       25 pts  (DPS growth rate — XBRL or inferred)
  FCF consistency       20 pts  (years with positive FCF in last 5)
"""

from __future__ import annotations

import logging
import statistics

from incomos.core.config import get_settings
from incomos.core.types import XBRLMetrics

logger = logging.getLogger(__name__)


def _count_dividend_years(metrics: list[XBRLMetrics]) -> int:
    return sum(
        1 for m in metrics
        if (m.dividends_paid is not None and m.dividends_paid > 0)
        or (m.dps_declared is not None and m.dps_declared > 0)
        or (m.dps_paid is not None and m.dps_paid > 0)
    )


def _dividend_growth_rate(metrics: list[XBRLMetrics]) -> float | None:
    """Estimate annualised DPS growth rate using available DPS data, or infer
    from dividends_paid trend if DPS is not available."""
    # Try DPS declared first
    dps_series = [m.dps_declared for m in metrics if m.dps_declared is not None and m.dps_declared > 0]
    if len(dps_series) >= 2:
        n = len(dps_series) - 1
        if dps_series[0] > 0:
            return (dps_series[-1] / dps_series[0]) ** (1 / n) - 1
    # Fallback: infer from dividends_paid
    paid = [m.dividends_paid for m in metrics if m.dividends_paid is not None and m.dividends_paid > 0]
    if len(paid) >= 2:
        n = len(paid) - 1
        if paid[0] > 0:
            return (paid[-1] / paid[0]) ** (1 / n) - 1
    return None


def compute_income_quality(metrics: list[XBRLMetrics]) -> tuple[float, dict]:
    """Return (score 0-100, breakdown dict).

    Missing data components receive 0 pts.  There is no partial credit.
    """
    if not metrics:
        return 0.0, {"error": "No XBRL data"}

    cfg = get_settings().income_q
    recent = sorted(metrics, key=lambda m: m.fiscal_year)[-5:]
    breakdown: dict = {}
    score = 0.0

    # 1. Dividend continuity (30 pts)
    div_years = _count_dividend_years(recent)
    continuity_pts = min(30, (div_years / max(len(recent), 1)) * 30)
    score += continuity_pts
    breakdown["dividend_continuity"] = {
        "years": div_years, "out_of": len(recent), "pts": round(continuity_pts, 1)
    }

    # 2. FCF payout ratio (25 pts) — 0 if data missing
    latest_with_payout = next(
        (m for m in reversed(recent) if m.fcf_payout_ratio is not None), None
    )
    if latest_with_payout:
        pr = latest_with_payout.fcf_payout_ratio
        if pr < cfg.payout_t1_max:   payout_pts = cfg.payout_t1_pts
        elif pr < cfg.payout_t2_max: payout_pts = cfg.payout_t2_pts
        elif pr < cfg.payout_t3_max: payout_pts = cfg.payout_t3_pts
        elif pr < cfg.payout_t4_max: payout_pts = cfg.payout_t4_pts
        else:                        payout_pts = 0.0
        breakdown["fcf_payout_ratio"] = {"ratio": round(pr, 3), "pts": payout_pts}
    else:
        payout_pts = 0.0
        breakdown["fcf_payout_ratio"] = {"ratio": None, "pts": 0.0, "note": "data gap"}
    score += payout_pts

    # 3. Dividend growth (25 pts) — 0 if uncomputable
    growth_rate = _dividend_growth_rate(recent)
    if growth_rate is not None:
        if growth_rate > cfg.growth_t1_min:   growth_pts = cfg.growth_t1_pts
        elif growth_rate > cfg.growth_t2_min: growth_pts = cfg.growth_t2_pts
        elif growth_rate > cfg.growth_t3_min: growth_pts = cfg.growth_t3_pts
        elif growth_rate > cfg.growth_t4_min: growth_pts = cfg.growth_t4_pts
        elif growth_rate > cfg.growth_t5_min: growth_pts = cfg.growth_t5_pts
        else:                                 growth_pts = 0.0
        breakdown["dividend_growth"] = {"cagr": round(growth_rate, 4), "pts": growth_pts}
    else:
        growth_pts = 0.0
        breakdown["dividend_growth"] = {"cagr": None, "pts": 0.0, "note": "data gap"}
    score += growth_pts

    # 4. FCF consistency (20 pts)
    fcf_pos_years = sum(1 for m in recent if m.free_cash_flow is not None and m.free_cash_flow > 0)
    consistency_pts = (fcf_pos_years / max(len(recent), 1)) * 20
    score += consistency_pts
    breakdown["fcf_consistency"] = {"positive_years": fcf_pos_years, "pts": round(consistency_pts, 1)}

    final = round(min(100, score), 1)
    breakdown["total"] = final
    return final, breakdown

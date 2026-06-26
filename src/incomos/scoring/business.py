"""Business Quality Score (0–100).

All scoring tiers read from config (no hardcoded thresholds).
Missing data components receive 0 pts — no partial credit.

Components:
  Revenue CAGR (3y)    25 pts
  FCF margin (latest)  25 pts
  FCF CAGR (3y)        25 pts
  Net debt trend       25 pts
"""

from __future__ import annotations

import logging

from incomos.core.config import get_settings
from incomos.core.types import XBRLMetrics

logger = logging.getLogger(__name__)


def _cagr(start: float, end: float, years: int) -> float | None:
    if start is None or end is None or years <= 0 or start <= 0:
        return None
    return (end / start) ** (1 / years) - 1


def compute_business_quality(metrics: list[XBRLMetrics]) -> tuple[float, dict]:
    """Return (score 0-100, breakdown dict).

    Missing data components score 0 — no partial credit — no benefit of doubt.
    """
    if not metrics:
        return 0.0, {"error": "No XBRL data"}

    cfg = get_settings().business_q
    recent = sorted(metrics, key=lambda m: m.fiscal_year)[-5:]
    score = 0.0
    breakdown: dict = {}

    # Revenue CAGR (25 pts) — 0 if missing
    rev_values = [(m.fiscal_year, m.revenue) for m in recent if m.revenue and m.revenue > 0]
    if len(rev_values) >= 2:
        start_yr, start_rev = rev_values[0]
        end_yr, end_rev = rev_values[-1]
        years = end_yr - start_yr or 1
        rev_cagr = _cagr(start_rev, end_rev, years)
        if rev_cagr is not None:
            if rev_cagr > cfg.rev_t1_min:   rev_pts = cfg.rev_t1_pts
            elif rev_cagr > cfg.rev_t2_min: rev_pts = cfg.rev_t2_pts
            elif rev_cagr > cfg.rev_t3_min: rev_pts = cfg.rev_t3_pts
            elif rev_cagr > cfg.rev_t4_min: rev_pts = cfg.rev_t4_pts
            else:                           rev_pts = cfg.rev_floor_pts
            breakdown["revenue_cagr"] = {"cagr": round(rev_cagr, 4), "pts": rev_pts}
        else:
            rev_pts = 0.0
            breakdown["revenue_cagr"] = {"cagr": None, "pts": 0.0}
    else:
        rev_pts = 0.0
        breakdown["revenue_cagr"] = {"pts": 0.0, "note": "data gap"}
    score += rev_pts

    # FCF Margin (25 pts) — 0 if missing
    latest = next(
        (m for m in reversed(recent)
         if m.free_cash_flow is not None and m.revenue is not None and m.revenue > 0),
        None,
    )
    if latest:
        fcf_margin = latest.free_cash_flow / latest.revenue
        if fcf_margin > cfg.margin_t1_min:   margin_pts = cfg.margin_t1_pts
        elif fcf_margin > cfg.margin_t2_min: margin_pts = cfg.margin_t2_pts
        elif fcf_margin > cfg.margin_t3_min: margin_pts = cfg.margin_t3_pts
        elif fcf_margin > cfg.margin_t4_min: margin_pts = cfg.margin_t4_pts
        elif fcf_margin > cfg.margin_t5_min: margin_pts = cfg.margin_t5_pts
        else:                                margin_pts = 0.0
        breakdown["fcf_margin"] = {"margin": round(fcf_margin, 4), "pts": margin_pts}
    else:
        margin_pts = 0.0
        breakdown["fcf_margin"] = {"pts": 0.0, "note": "data gap"}
    score += margin_pts

    # FCF CAGR (25 pts) — 0 if missing
    fcf_values = [(m.fiscal_year, m.free_cash_flow) for m in recent
                  if m.free_cash_flow is not None and m.free_cash_flow > 0]
    if len(fcf_values) >= 2:
        s_yr, s_fcf = fcf_values[0]
        e_yr, e_fcf = fcf_values[-1]
        years = e_yr - s_yr or 1
        fcf_cagr = _cagr(s_fcf, e_fcf, years)
        if fcf_cagr is not None:
            if fcf_cagr > cfg.fcf_cagr_t1_min:   fcf_pts = cfg.fcf_cagr_t1_pts
            elif fcf_cagr > cfg.fcf_cagr_t2_min: fcf_pts = cfg.fcf_cagr_t2_pts
            elif fcf_cagr > cfg.fcf_cagr_t3_min: fcf_pts = cfg.fcf_cagr_t3_pts
            elif fcf_cagr > cfg.fcf_cagr_t4_min: fcf_pts = cfg.fcf_cagr_t4_pts
            else:                                fcf_pts = cfg.fcf_cagr_floor_pts
            breakdown["fcf_cagr"] = {"cagr": round(fcf_cagr, 4), "pts": fcf_pts}
        else:
            fcf_pts = 0.0
            breakdown["fcf_cagr"] = {"cagr": None, "pts": 0.0}
    else:
        fcf_pts = 0.0
        breakdown["fcf_cagr"] = {"pts": 0.0, "note": "data gap"}
    score += fcf_pts

    # Net debt trend (25 pts) — 0 if missing
    net_debt_values = [m.net_debt for m in recent if m.net_debt is not None]
    if len(net_debt_values) >= 2:
        trend_delta = net_debt_values[-1] - net_debt_values[0]
        anchor = net_debt_values[0]
        if trend_delta < 0:
            debt_pts = cfg.net_debt_improve_pts
            trend_label = "improving"
        elif anchor != 0 and abs(trend_delta) < abs(anchor) * cfg.net_debt_stable_pct:
            debt_pts = cfg.net_debt_stable_pts
            trend_label = "stable"
        else:
            debt_pts = cfg.net_debt_worsen_pts
            trend_label = "worsening"
        breakdown["net_debt_trend"] = {
            "latest_mUSD": round(net_debt_values[-1] / 1e6, 0),
            "trend": trend_label, "pts": debt_pts,
        }
    else:
        debt_pts = 0.0
        breakdown["net_debt_trend"] = {"pts": 0.0, "note": "data gap"}
    score += debt_pts

    final = round(min(100, score), 1)
    breakdown["total"] = final
    return final, breakdown

"""Oversold Confidence Score (0–100).

All scoring tiers read from config (no hardcoded thresholds).
Missing RSI or volume scores 0 — no partial credit.

Components:
  RSI signal          40 pts
  % below 52W high    35 pts
  Volume signal       25 pts
"""

from __future__ import annotations

import logging

from incomos.core.config import get_settings
from incomos.core.exceptions import PriceDataUnavailableError
from incomos.data.market import PriceSnapshot

logger = logging.getLogger(__name__)


def compute_oversold_confidence(snap: PriceSnapshot) -> tuple[float, dict]:
    """Return (score 0-100, breakdown dict).

    Raises PriceDataUnavailableError if snap quality is FAILED.
    Missing RSI or volume score 0 — no partial credit.
    """
    if snap.data_quality == "FAILED":
        raise PriceDataUnavailableError(snap.ticker)

    cfg = get_settings().oversold_q
    breakdown: dict = {}
    score = 0.0

    # 1. RSI (40 pts) — lower RSI = more oversold; 0 if unavailable
    if snap.rsi_14 is not None:
        rsi = snap.rsi_14
        if rsi < cfg.rsi_t1:   rsi_pts = cfg.rsi_p1
        elif rsi < cfg.rsi_t2: rsi_pts = cfg.rsi_p2
        elif rsi < cfg.rsi_t3: rsi_pts = cfg.rsi_p3
        elif rsi < cfg.rsi_t4: rsi_pts = cfg.rsi_p4
        elif rsi < cfg.rsi_t5: rsi_pts = cfg.rsi_p5
        elif rsi < cfg.rsi_t6: rsi_pts = cfg.rsi_p6
        elif rsi < cfg.rsi_t7: rsi_pts = cfg.rsi_p7
        else:                  rsi_pts = 0.0
        breakdown["rsi"] = {"value": rsi, "pts": rsi_pts}
    else:
        rsi_pts = 0.0
        breakdown["rsi"] = {"value": None, "pts": 0.0, "note": "data gap"}
    score += rsi_pts

    # 2. % below 52W high (35 pts)
    pct = snap.pct_below_52w_high
    if pct >= cfg.pct_t1:   price_pts = cfg.pct_p1
    elif pct >= cfg.pct_t2: price_pts = cfg.pct_p2
    elif pct >= cfg.pct_t3: price_pts = cfg.pct_p3
    elif pct >= cfg.pct_t4: price_pts = cfg.pct_p4
    elif pct >= cfg.pct_t5: price_pts = cfg.pct_p5
    elif pct >= cfg.pct_t6: price_pts = cfg.pct_p6
    else:                   price_pts = 0.0
    breakdown["pct_below_52w"] = {"value": round(pct, 4), "pts": price_pts}
    score += price_pts

    # 3. Volume ratio (25 pts) — 0 if unavailable
    if snap.volume_ratio is not None:
        vr = snap.volume_ratio
        if vr >= cfg.vol_t1:   vol_pts = cfg.vol_p1
        elif vr >= cfg.vol_t2: vol_pts = cfg.vol_p2
        elif vr >= cfg.vol_t3: vol_pts = cfg.vol_p3
        elif vr >= cfg.vol_t4: vol_pts = cfg.vol_p4
        elif vr >= cfg.vol_t5: vol_pts = cfg.vol_p5
        else:                  vol_pts = 0.0
        breakdown["volume_ratio"] = {"value": round(vr, 2), "pts": vol_pts}
    else:
        vol_pts = 0.0
        breakdown["volume_ratio"] = {"value": None, "pts": 0.0, "note": "data gap"}
    score += vol_pts

    final = round(min(100, score), 1)
    breakdown["total"] = final
    return final, breakdown

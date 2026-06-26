"""Stage 1→2 dip trigger screen — rule-based, no LLM.

A stock in the PROSPECTS pool is promoted to KIV when a meaningful dip
is detected. The screen uses three signals:

  1. Price drawdown: how far below the 52W high (primary)
  2. RSI: momentum exhaustion signal (secondary)
  3. Volume: abnormal selling pressure (context)

Thresholds are configurable. Defaults are conservative to minimize
false positives — better to miss a small dip than to waste KIV capacity
on shallow pullbacks.

The dip trigger does NOT call MiMo 2.5. Cost must remain near-zero here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from incomos.core.config import get_settings
from incomos.data.market import PriceSnapshot

logger = logging.getLogger(__name__)


@dataclass
class DipTriggerResult:
    ticker: str
    triggered: bool
    trigger_strength: str           # STRONG | MODERATE | WEAK | NONE
    pct_below_52w_high: float
    rsi: float | None
    volume_ratio: float | None
    signals: list[str]
    data_quality: str


def check_dip_trigger(snap: PriceSnapshot) -> DipTriggerResult:
    """Evaluate whether a PROSPECTS stock should be promoted to KIV.

    All thresholds are read from config (no hardcoded constants).
    Raises PriceDataUnavailableError if the snapshot has FAILED quality —
    callers are responsible for ensuring clean price data.
    """
    from incomos.core.exceptions import PriceDataUnavailableError
    if snap.data_quality == "FAILED":
        raise PriceDataUnavailableError(snap.ticker)

    cfg = get_settings().dip_trigger
    signals: list[str] = []
    price_score = 0
    rsi_score = 0
    vol_score = 0

    pct = snap.pct_below_52w_high
    if pct >= cfg.threshold_hard:
        price_score = 2
        signals.append(f"Price {pct:.1%} below 52W high (hard ≥{cfg.threshold_hard:.0%})")
    elif pct >= cfg.threshold_soft:
        price_score = 1
        signals.append(f"Price {pct:.1%} below 52W high (soft ≥{cfg.threshold_soft:.0%})")
    else:
        signals.append(f"Price {pct:.1%} below 52W high — no trigger")

    if snap.rsi_14 is not None:
        rsi = snap.rsi_14
        if rsi < cfg.rsi_hard:
            rsi_score = 2
            signals.append(f"RSI {rsi:.1f} (exhausted, < {cfg.rsi_hard:.0f})")
        elif rsi < cfg.rsi_soft:
            rsi_score = 1
            signals.append(f"RSI {rsi:.1f} (weakening, < {cfg.rsi_soft:.0f})")

    if snap.volume_ratio is not None:
        vr = snap.volume_ratio
        if vr >= cfg.volume_elevated:
            vol_score = 1
            signals.append(f"Volume ratio {vr:.2f}x (≥{cfg.volume_elevated:.1f}x elevated)")

    # Price trend signal: TRENDING_DOWN adds a point even when RSI isn't exhausted.
    # Dividend/value stocks often have higher RSI floors during pullbacks —
    # the trend signal catches them without loosening the RSI gate for all stocks.
    trend_score = 0
    if snap.price_trend == "TRENDING_DOWN":
        trend_score = 1
        signals.append(f"Price trend: TRENDING_DOWN")

    total = price_score + rsi_score + vol_score + trend_score

    if price_score == 2 and rsi_score >= 1:
        strength = "STRONG"
    elif price_score >= 1 and rsi_score >= 1:
        strength = "MODERATE"
    elif price_score >= 1 and trend_score >= 1:
        strength = "MODERATE"
    elif price_score >= 1:
        strength = "WEAK"
    else:
        strength = "NONE"

    triggered = total >= 2 or price_score == 2

    return DipTriggerResult(
        ticker=snap.ticker,
        triggered=triggered,
        trigger_strength=strength,
        pct_below_52w_high=pct,
        rsi=snap.rsi_14,
        volume_ratio=snap.volume_ratio,
        signals=signals,
        data_quality=snap.data_quality,
    )

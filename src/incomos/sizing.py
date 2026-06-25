"""Position sizer — yield-aware, MYR base currency.

All constants read from config (no hardcoded values).
Raises FXRateUnavailableError for USD stocks when FRED DEXMAUS is unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from incomos.core.config import get_settings
from incomos.core.exceptions import FXRateUnavailableError
from incomos.scoring.engine import ScoringResult

logger = logging.getLogger(__name__)


@dataclass
class PositionSizing:
    ticker: str
    exchange: str                     # US | MY
    base_position_myr: float
    score_multiplier: float
    annotation_multiplier: float      # 1.0 default, up to 1.5 with human override
    adjusted_position_myr: float
    income_cap_applied: bool
    usd_myr_rate: float | None
    position_usd: float | None        # populated for US stocks
    notes: list[str]


def compute_position_size(
    result: ScoringResult,
    portfolio_size_myr: float,
    exchange: str = "US",
    usd_myr_rate: float | None = None,
    dividend_yield_pct: float | None = None,
    portfolio_annual_income_myr: float | None = None,
    annotation_multiplier: float = 1.0,
) -> PositionSizing:
    """Compute position size in MYR.

    All thresholds (base_allocation_pct, max_income_contribution, annotation_max)
    are read from config.  Raises FXRateUnavailableError for US stocks if
    usd_myr_rate is None (Gap D: no hardcoded fallback).
    """
    cfg = get_settings().sizing
    mult_cfg = get_settings().score_multiplier
    notes: list[str] = []

    annotation_multiplier = min(annotation_multiplier, mult_cfg.annotation_max)

    if exchange == "US" and usd_myr_rate is None:
        raise FXRateUnavailableError()

    base = portfolio_size_myr * cfg.base_allocation_pct
    adjusted = base * result.base_size_multiplier * annotation_multiplier

    income_cap_applied = False
    if (
        dividend_yield_pct is not None
        and portfolio_annual_income_myr is not None
        and portfolio_annual_income_myr > 0
    ):
        income_from_position = adjusted * dividend_yield_pct
        income_cap = portfolio_annual_income_myr * cfg.max_income_contribution
        if income_from_position > income_cap:
            adjusted = income_cap / dividend_yield_pct
            income_cap_applied = True
            notes.append(
                f"Income cap applied: capped at {cfg.max_income_contribution:.0%} of portfolio income. "
                f"Position reduced to MYR {adjusted:,.0f}."
            )

    position_usd = (adjusted / usd_myr_rate) if exchange == "US" and usd_myr_rate else None

    return PositionSizing(
        ticker=result.ticker,
        exchange=exchange,
        base_position_myr=round(base, 2),
        score_multiplier=result.base_size_multiplier,
        annotation_multiplier=annotation_multiplier,
        adjusted_position_myr=round(adjusted, 2),
        income_cap_applied=income_cap_applied,
        usd_myr_rate=usd_myr_rate,
        position_usd=round(position_usd, 2) if position_usd else None,
        notes=notes,
    )

"""Stage 0→1 quality screen — rule-based, no LLM, near-zero cost.

This is the first funnel gate. The screen must stay cheap. No XBRL re-fetching
happens here — it operates on already-extracted XBRLMetrics.

Pass criteria (all configurable via settings):
  1. Dividend paid continuously for min_dividend_years
  2. FCF positive in at least min_fcf_positive_years of the last 5 years
  3. FCF payout ratio below max_fcf_payout_ratio (in the most recent year with data)
  4. Revenue non-negative in most recent year (basic sanity check)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from incomos.core.config import get_settings
from incomos.core.types import XBRLMetrics

logger = logging.getLogger(__name__)


@dataclass
class ScreenResult:
    """Result of the Stage 0→1 quality screen."""
    ticker: str
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        passing = sum(1 for v in self.checks.values() if v)
        total = len(self.checks)
        return f"{self.ticker}: {status} ({passing}/{total} checks passed)"


def run_quality_screen(ticker: str, metrics: list[XBRLMetrics]) -> ScreenResult:
    """Run the Stage 0→1 quality screen against extracted XBRL metrics.

    Returns a ScreenResult with per-check outcomes and notes.
    Stocks that pass → promoted to PROSPECTS pool.
    Stocks that fail → REJECTED with reason recorded.
    """
    cfg = get_settings()
    checks: dict[str, bool] = {}
    notes: list[str] = []

    if not metrics:
        return ScreenResult(
            ticker=ticker,
            passed=False,
            checks={"data_available": False},
            notes=["No XBRL data found — cannot screen."],
        )

    # Sort oldest → newest
    sorted_metrics = sorted(metrics, key=lambda m: m.fiscal_year)
    recent = sorted_metrics[-5:]  # At most 5 years
    latest = sorted_metrics[-1]

    # Gap G: regulated utilities have structurally negative FCF due to mandated capex.
    # Use a relaxed FCF threshold when the SIC code indicates a regulated utility.
    sic = latest.sic_code or ""
    is_utility = (
        sic.isdigit()
        and cfg.utility_sic_min <= int(sic) <= cfg.utility_sic_max
    )
    effective_min_fcf_years = (
        cfg.min_fcf_positive_years_utility if is_utility else cfg.min_fcf_positive_years
    )
    if is_utility:
        notes.append(f"Regulated utility detected (SIC {sic}) -- FCF threshold relaxed to ≥{effective_min_fcf_years} of {len(recent)} years.")

    # ------------------------------------------------------------------
    # Check 1: Dividend continuity
    # A year counts as a "dividend year" if ANY of these is positive:
    #   dividends_paid, dps_declared, dps_paid
    # This handles XBRL tag gaps (some companies change tags across years —
    # None means data not found, which is different from 0 = confirmed no dividend).
    # ------------------------------------------------------------------
    def _year_has_dividend(m: XBRLMetrics) -> bool:
        return (
            (m.dividends_paid is not None and m.dividends_paid > 0)
            or (m.dps_declared is not None and m.dps_declared > 0)
            or (m.dps_paid is not None and m.dps_paid > 0)
        )

    dividend_years = [m for m in recent if _year_has_dividend(m)]
    data_missing_years = [
        m for m in recent
        if m.dividends_paid is None and m.dps_declared is None and m.dps_paid is None
    ]

    checks["dividend_continuity"] = len(dividend_years) >= cfg.min_dividend_years
    if not checks["dividend_continuity"]:
        msg = (
            f"Dividend confirmed in only {len(dividend_years)} of last {len(recent)} years "
            f"(need ≥{cfg.min_dividend_years})."
        )
        if data_missing_years:
            msg += (
                f" Note: dividend data missing (XBRL tag gap) for "
                f"FY{', FY'.join(str(m.fiscal_year) for m in data_missing_years)} — "
                f"may need manual verification."
            )
        notes.append(msg)

    # ------------------------------------------------------------------
    # Check 2: FCF positivity
    # ------------------------------------------------------------------
    fcf_positive_years = [
        m for m in recent
        if m.free_cash_flow is not None and m.free_cash_flow > 0
    ]
    checks["fcf_positive"] = len(fcf_positive_years) >= effective_min_fcf_years
    if not checks["fcf_positive"]:
        notes.append(
            f"FCF positive in only {len(fcf_positive_years)} of last {len(recent)} years "
            f"(need ≥{effective_min_fcf_years})."
        )

    # ------------------------------------------------------------------
    # Check 3: FCF payout ratio (most recent year with both values)
    # ------------------------------------------------------------------
    payout_check_year = next(
        (m for m in reversed(sorted_metrics) if m.fcf_payout_ratio is not None),
        None,
    )
    if payout_check_year is not None:
        ratio = payout_check_year.fcf_payout_ratio
        checks["fcf_payout_ratio"] = ratio <= cfg.max_fcf_payout_ratio  # type: ignore[operator]
        if not checks["fcf_payout_ratio"]:
            notes.append(
                f"FCF payout ratio {ratio:.1%} in FY{payout_check_year.fiscal_year} "
                f"exceeds ceiling {cfg.max_fcf_payout_ratio:.0%}."
            )
    else:
        # Can't compute — FCF or dividends data missing
        checks["fcf_payout_ratio"] = False
        notes.append("Cannot compute FCF payout ratio — missing FCF or dividends data.")

    # ------------------------------------------------------------------
    # Check 4: Revenue non-negative (basic sanity)
    # ------------------------------------------------------------------
    if latest.revenue is not None:
        checks["revenue_positive"] = latest.revenue > 0
        if not checks["revenue_positive"]:
            notes.append(f"Revenue negative or zero in FY{latest.fiscal_year}.")
    else:
        checks["revenue_positive"] = False
        notes.append("Revenue not available in XBRL data.")

    passed = all(checks.values())
    if passed:
        notes.append("All quality checks passed → eligible for PROSPECTS pool.")
    else:
        failed = [k for k, v in checks.items() if not v]
        notes.append(f"Failed checks: {', '.join(failed)}.")

    return ScreenResult(ticker=ticker, passed=passed, checks=checks, notes=notes)

"""Forward-return validation — Gap B.

Validates whether IncomOS Opportunity Scores predict actual subsequent returns.

Gap B (architecture open item):
  The specific forward-return success threshold has NOT been defined.
  ALL thresholds in this module are parameters.  Never hardcode a cutoff.

Calibration workflow:
  1. As the system accumulates scored entries in research_store, call
     ForwardReturnValidator.record_entry() to log each entry.
  2. Periodically run calibration_report() to evaluate whether score buckets
     (0–60, 60–70, 70–80, 80+) produce meaningfully different forward returns.
  3. Adjust scoring weights in response (Phase 2→3 gate).

Benchmark:
  - US stocks: ^GSPC (S&P 500)
  - MY stocks: ^KLSE (FTSE Bursa Malaysia KLCI)
  The benchmark is configurable to account for currency effects (MYR base).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_US_BENCHMARK = "^GSPC"
_MY_BENCHMARK = "^KLSE"

# Score bucket boundaries — configurable at call site (Gap B rule)
_DEFAULT_BUCKETS = [0, 60, 70, 80, 100]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ForwardReturnResult:
    """Actual price return for one ticker over a given horizon from entry."""

    ticker: str
    entry_date: str           # YYYY-MM-DD
    entry_price: float
    horizon_days: int
    exit_date: str | None     # None if horizon has not elapsed yet
    exit_price: float | None  # None if horizon has not elapsed yet
    return_pct: float | None  # (exit_price / entry_price) - 1
    benchmark_return_pct: float | None  # benchmark return over same window
    vs_benchmark_pct: float | None      # return_pct - benchmark_return_pct
    exchange: str = "US"      # US | MY


@dataclass
class ScoreBucketStats:
    """Aggregated statistics for one opportunity score bucket."""

    bucket_label: str          # e.g. "60–70"
    score_min: float
    score_max: float
    n: int                     # number of observations
    avg_return_pct: float | None
    median_return_pct: float | None
    avg_vs_benchmark_pct: float | None
    hit_rate: float | None     # fraction of observations above threshold (caller-supplied)
    threshold_used: float | None  # the threshold against which hit_rate was computed


@dataclass
class CalibrationReport:
    """Score calibration report across all available forward-return observations."""

    generated_at: str          # ISO timestamp
    horizon_days: int          # which horizon this report covers
    n_total: int
    bucket_stats: list[ScoreBucketStats]
    # Gap B: overall success threshold is NOT set here — see threshold_used in buckets
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class ForwardReturnValidator:
    """Compute and aggregate forward returns for IncomOS scored entries.

    Usage (single entry):
        validator = ForwardReturnValidator()
        result = validator.compute_forward_return(
            ticker="MCD",
            entry_date="2025-06-01",
            entry_price=290.0,
            horizons=[30, 90, 180, 365],
        )

    Usage (calibration report, requires historical scored entries DataFrame):
        report = validator.calibration_report(
            historical_df=df,     # columns: ticker, entry_date, entry_price,
                                  #          opportunity_score, exchange
            horizon_days=90,
            threshold=0.05,       # Gap B: caller provides threshold, not hardcoded
        )
    """

    # ------------------------------------------------------------------
    # Single-entry forward return
    # ------------------------------------------------------------------

    def compute_forward_return(
        self,
        ticker: str,
        entry_date: str,
        entry_price: float,
        horizons: list[int] | None = None,
        exchange: str = "US",
    ) -> list[ForwardReturnResult]:
        """Compute price return at each horizon from entry_date.

        Returns one ForwardReturnResult per horizon.  If a horizon has not yet
        elapsed (exit date > today), exit_price and return_pct are None.
        """
        if horizons is None:
            horizons = [30, 90, 180, 365]

        benchmark = _MY_BENCHMARK if exchange == "MY" else _US_BENCHMARK
        entry_dt = datetime.strptime(entry_date, "%Y-%m-%d").date()
        today = date.today()

        results: list[ForwardReturnResult] = []
        for h in horizons:
            exit_dt = entry_dt + timedelta(days=h)
            elapsed = exit_dt <= today

            if not elapsed:
                results.append(ForwardReturnResult(
                    ticker=ticker,
                    entry_date=entry_date,
                    entry_price=entry_price,
                    horizon_days=h,
                    exit_date=None,
                    exit_price=None,
                    return_pct=None,
                    benchmark_return_pct=None,
                    vs_benchmark_pct=None,
                    exchange=exchange,
                ))
                continue

            exit_price = self._get_price_on(ticker, exit_dt)
            bench_entry = self._get_price_on(benchmark, entry_dt)
            bench_exit = self._get_price_on(benchmark, exit_dt)

            ret = (exit_price / entry_price - 1) if exit_price else None
            bench_ret = (
                (bench_exit / bench_entry - 1)
                if bench_entry and bench_exit
                else None
            )
            vs_bench = (ret - bench_ret) if ret is not None and bench_ret is not None else None

            results.append(ForwardReturnResult(
                ticker=ticker,
                entry_date=entry_date,
                entry_price=entry_price,
                horizon_days=h,
                exit_date=exit_dt.isoformat(),
                exit_price=exit_price,
                return_pct=round(ret, 4) if ret is not None else None,
                benchmark_return_pct=round(bench_ret, 4) if bench_ret is not None else None,
                vs_benchmark_pct=round(vs_bench, 4) if vs_bench is not None else None,
                exchange=exchange,
            ))

        return results

    # ------------------------------------------------------------------
    # Calibration report
    # ------------------------------------------------------------------

    def calibration_report(
        self,
        historical_df: pd.DataFrame,
        horizon_days: int = 90,
        threshold: float | None = None,
        bucket_boundaries: list[float] | None = None,
    ) -> CalibrationReport:
        """Produce a calibration report from a DataFrame of historical scored entries.

        Required columns in historical_df:
          ticker, entry_date (YYYY-MM-DD), entry_price, opportunity_score, exchange

        threshold (Gap B):
          Caller-supplied return threshold for hit_rate calculation.
          If None, hit_rate will be None in the report — do NOT default to a value.

        bucket_boundaries:
          List of score breakpoints, default [0, 60, 70, 80, 100].
        """
        if bucket_boundaries is None:
            bucket_boundaries = _DEFAULT_BUCKETS

        required_cols = {"ticker", "entry_date", "entry_price", "opportunity_score", "exchange"}
        missing = required_cols - set(historical_df.columns)
        if missing:
            raise ValueError(f"calibration_report: missing columns {missing}")

        # Compute forward returns for all rows
        rows_with_returns: list[dict] = []
        for _, row in historical_df.iterrows():
            fr_list = self.compute_forward_return(
                ticker=row["ticker"],
                entry_date=row["entry_date"],
                entry_price=float(row["entry_price"]),
                horizons=[horizon_days],
                exchange=row.get("exchange", "US"),
            )
            if fr_list and fr_list[0].return_pct is not None:
                rows_with_returns.append({
                    "opportunity_score": float(row["opportunity_score"]),
                    "return_pct": fr_list[0].return_pct,
                    "vs_benchmark_pct": fr_list[0].vs_benchmark_pct,
                })

        if not rows_with_returns:
            return CalibrationReport(
                generated_at=datetime.utcnow().isoformat(),
                horizon_days=horizon_days,
                n_total=0,
                bucket_stats=[],
                notes=["No completed forward-return observations available yet."],
            )

        df = pd.DataFrame(rows_with_returns)

        bucket_stats: list[ScoreBucketStats] = []
        for i in range(len(bucket_boundaries) - 1):
            lo, hi = bucket_boundaries[i], bucket_boundaries[i + 1]
            mask = (df["opportunity_score"] >= lo) & (df["opportunity_score"] < hi)
            subset = df[mask]
            n = len(subset)

            avg_ret = float(subset["return_pct"].mean()) if n > 0 else None
            med_ret = float(subset["return_pct"].median()) if n > 0 else None
            avg_vs = float(subset["vs_benchmark_pct"].mean()) if n > 0 and "vs_benchmark_pct" in subset else None

            # Gap B: hit_rate only computed if threshold is explicitly provided
            hit_rate: float | None = None
            if threshold is not None and n > 0:
                hit_rate = float((subset["return_pct"] > threshold).mean())

            bucket_stats.append(ScoreBucketStats(
                bucket_label=f"{lo:.0f}–{hi:.0f}",
                score_min=lo,
                score_max=hi,
                n=n,
                avg_return_pct=round(avg_ret, 4) if avg_ret is not None else None,
                median_return_pct=round(med_ret, 4) if med_ret is not None else None,
                avg_vs_benchmark_pct=round(avg_vs, 4) if avg_vs is not None else None,
                hit_rate=round(hit_rate, 4) if hit_rate is not None else None,
                threshold_used=threshold,
            ))

        notes = []
        if threshold is None:
            notes.append(
                "Gap B: forward-return success threshold not defined — "
                "hit_rate is None.  Provide threshold= when ready to calibrate."
            )

        return CalibrationReport(
            generated_at=datetime.utcnow().isoformat(),
            horizon_days=horizon_days,
            n_total=len(df),
            bucket_stats=bucket_stats,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # Internal: price lookup via yfinance
    # ------------------------------------------------------------------

    def _get_price_on(self, ticker: str, target_date: date) -> float | None:
        """Return the closing price on or just after target_date."""
        try:
            start = target_date.isoformat()
            end = (target_date + timedelta(days=5)).isoformat()  # buffer for non-trading days
            hist = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
            if hist.empty:
                return None
            return float(hist["Close"].iloc[0])
        except Exception as exc:
            logger.warning("ForwardReturnValidator: price lookup failed for %s on %s: %s",
                           ticker, target_date, exc)
            return None

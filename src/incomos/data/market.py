"""Market data client — price, technicals, and dip signal computation.

Uses yfinance as the data source (free, no API key required).
All computations are deterministic / rule-based. No LLM involved.

Provides:
  - 52-week high / low
  - Current price and % below 52W high
  - RSI (14-period)
  - Volume ratio (5-day avg / 20-day avg)
  - Price trend (above/below 50-day and 200-day SMA)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class PriceSnapshot:
    """Current price and technical indicators for one ticker."""
    ticker: str
    current_price: float
    week52_high: float
    week52_low: float
    pct_below_52w_high: float       # 0.20 = 20% below peak
    rsi_14: float | None            # None if insufficient history
    volume_ratio: float | None      # 5d avg volume / 20d avg volume
    price_above_50sma: bool | None
    price_above_200sma: bool | None
    price_trend: str                # TRENDING_UP | TRENDING_DOWN | RANGING
    data_quality: str               # GOOD | PARTIAL | FAILED
    # Yield data for entry attractiveness scoring
    current_yield: float | None = None          # Current annualized dividend yield
    avg_yield_5yr: float | None = None          # 5-year average dividend yield
    yield_expansion_ratio: float | None = None  # current_yield / avg_yield_5yr


def _compute_rsi(closes: pd.Series, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    avg_gain = gains.rolling(window=period, min_periods=period).mean().iloc[-1]
    avg_loss = losses.rolling(window=period, min_periods=period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _compute_price_trend(closes: pd.Series) -> tuple[bool | None, bool | None, str]:
    """Returns (above_50sma, above_200sma, trend_label)."""
    if len(closes) < 20:
        return None, None, "RANGING"
    current = closes.iloc[-1]
    sma50 = closes.rolling(50, min_periods=20).mean().iloc[-1]
    sma200 = closes.rolling(200, min_periods=50).mean().iloc[-1] if len(closes) >= 50 else None

    above_50 = bool(current > sma50) if not pd.isna(sma50) else None
    above_200 = bool(current > sma200) if sma200 is not None and not pd.isna(sma200) else None

    if above_50 is True and (above_200 is True or above_200 is None):
        trend = "TRENDING_UP"
    elif above_50 is False and (above_200 is False or above_200 is None):
        trend = "TRENDING_DOWN"
    else:
        trend = "RANGING"
    return above_50, above_200, trend


def _compute_dividend_yield_history(
    ticker_obj: yf.Ticker, closes: pd.Series
) -> tuple[float | None, float | None, float | None]:
    """Compute current yield, 5-year average yield, and yield expansion ratio.

    Uses yfinance dividends + close prices to compute trailing 12-month yield
    at each point in time, then averages over the available history (up to 5 years).

    Returns (current_yield, avg_yield_5yr, yield_expansion_ratio).
    Any value is None if insufficient data.
    """
    try:
        dividends = ticker_obj.dividends
        if dividends is None or dividends.empty or closes.empty:
            return None, None, None

        # Align dividend index to close price index
        # dividends Series has DatetimeIndex (ex-dividend dates)
        # We need trailing 12-month dividends at each price date
        if closes.index.tz is not None and dividends.index.tz is None:
            dividends.index = dividends.index.tz_localize(closes.index.tz)
        elif closes.index.tz is None and dividends.index.tz is not None:
            closes = closes.tz_localize(None)
            dividends.index = dividends.index.tz_localize(None)

        # Compute trailing 12-month dividend sum at each close price date
        # Use a rolling window approach: for each date, sum dividends in the prior 365 days
        current_price = float(closes.iloc[-1])
        if current_price <= 0:
            return None, None, None

        # Trailing 12-month dividends ending at the latest close date
        latest_date = closes.index[-1]
        one_year_ago = latest_date - pd.Timedelta(days=365)
        ttm_divs = dividends[(dividends.index > one_year_ago) & (dividends.index <= latest_date)]
        ttm_div_total = float(ttm_divs.sum()) if not ttm_divs.empty else 0.0
        current_yield = ttm_div_total / current_price if current_price > 0 else None

        # 5-year average yield: compute yield at yearly intervals
        # For each year in the past 5 years, compute trailing 12m yield
        yields: list[float] = []
        for years_back in range(1, 6):
            target_date = latest_date - pd.Timedelta(days=365 * years_back)
            # Find closest available close price
            mask = closes.index <= target_date
            if mask.sum() == 0:
                continue
            price_at = float(closes.loc[mask].iloc[-1])
            price_date = closes.index[mask][-1]

            if price_at <= 0:
                continue

            # TTM dividends ending at that date
            ttm_start = price_date - pd.Timedelta(days=365)
            ttm_at = dividends[(dividends.index > ttm_start) & (dividends.index <= price_date)]
            ttm_total = float(ttm_at.sum()) if not ttm_at.empty else 0.0
            if ttm_total > 0:
                yields.append(ttm_total / price_at)

        # Include current yield in the average
        if current_yield is not None and current_yield > 0:
            yields.append(current_yield)

        avg_yield_5yr = sum(yields) / len(yields) if yields else None
        yield_expansion = (
            current_yield / avg_yield_5yr
            if current_yield and avg_yield_5yr and avg_yield_5yr > 0
            else None
        )

        return current_yield, avg_yield_5yr, yield_expansion

    except Exception:
        return None, None, None


def get_price_snapshot(ticker: str) -> PriceSnapshot:
    """Fetch current price snapshot for a ticker via yfinance.

    Raises PriceDataUnavailableError if data cannot be fetched or is empty.
    Returns a PriceSnapshot with quality GOOD or PARTIAL (never FAILED).
    """
    from incomos.core.exceptions import PriceDataUnavailableError
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1y", interval="1d", auto_adjust=True)
        if hist.empty:
            raise PriceDataUnavailableError(ticker)

        closes = hist["Close"]
        volumes = hist["Volume"]
        current = float(closes.iloc[-1])
        high52 = float(closes.max())
        low52 = float(closes.min())
        pct_below = (high52 - current) / high52 if high52 > 0 else 0.0

        rsi = _compute_rsi(closes)

        vol_5d = float(volumes.iloc[-5:].mean()) if len(volumes) >= 5 else None
        vol_20d = float(volumes.iloc[-20:].mean()) if len(volumes) >= 20 else None
        vol_ratio = (vol_5d / vol_20d) if vol_5d and vol_20d and vol_20d > 0 else None

        above_50, above_200, trend = _compute_price_trend(closes)

        # Compute dividend yield data for entry attractiveness scoring
        current_yield, avg_yield_5yr, yield_expansion = _compute_dividend_yield_history(t, closes)

        quality = "GOOD" if rsi is not None and vol_ratio is not None else "PARTIAL"
        logger.debug("%s: price=%.2f 52H=%.2f pct_below=%.1f%% RSI=%.1f yield=%.2f%%",
                     ticker, current, high52, pct_below * 100, rsi or 0,
                     (current_yield or 0) * 100)

        return PriceSnapshot(
            ticker=ticker, current_price=current,
            week52_high=high52, week52_low=low52,
            pct_below_52w_high=pct_below,
            rsi_14=rsi, volume_ratio=vol_ratio,
            price_above_50sma=above_50, price_above_200sma=above_200,
            price_trend=trend, data_quality=quality,
            current_yield=current_yield,
            avg_yield_5yr=avg_yield_5yr,
            yield_expansion_ratio=yield_expansion,
        )
    except PriceDataUnavailableError:
        raise
    except Exception as exc:
        raise PriceDataUnavailableError(ticker) from exc

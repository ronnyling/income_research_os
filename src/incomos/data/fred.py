"""FRED API client — macro economic time series.

All series used here are public (no API key required for CSV download).
Falls back gracefully to UNKNOWN state on network/parsing failure.

Series used:
  DGS2           — 2-year Treasury constant maturity rate (daily)
  DGS10          — 10-year Treasury constant maturity rate (daily)
  NFCI           — Chicago Fed National Financial Conditions Index (weekly)
  ANFCI          — Adjusted NFCI (weekly)
  DEXMAUS        — USD/MYR exchange rate (daily, FRED series)
"""

from __future__ import annotations

import io
import logging
from datetime import date, timedelta
from typing import Any

import httpx
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

_FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
def _fetch_series(series_id: str, observation_start: str | None = None) -> pd.DataFrame:
    """Download a FRED series as a DataFrame with columns [date, value]."""
    params: dict[str, Any] = {"id": series_id}
    if observation_start:
        params["vintage_date"] = observation_start
    with httpx.Client(timeout=30) as client:
        resp = client.get(_FRED_CSV_URL, params=params)
        resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    # FRED CSVs have first column as DATE, second as the series value
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["date", "value"])
    return df


def get_latest_value(series_id: str, lookback_days: int = 30) -> float:
    """Return the most recent non-NaN value for a FRED series.

    Raises MacroDataUnavailableError if the series cannot be fetched or is empty.
    """
    from incomos.core.exceptions import MacroDataUnavailableError
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    try:
        df = _fetch_series(series_id, observation_start=start)
    except Exception as exc:
        raise MacroDataUnavailableError(series_id, str(exc)) from exc
    if df.empty:
        raise MacroDataUnavailableError(series_id, f"no data in last {lookback_days} days")
    return float(df["value"].iloc[-1])


def get_recent_series(series_id: str, n: int = 30) -> list[tuple[date, float]]:
    """Return the last N observations for a FRED series.

    Raises MacroDataUnavailableError if the series cannot be fetched.
    """
    from incomos.core.exceptions import MacroDataUnavailableError
    start = (date.today() - timedelta(days=n * 2)).isoformat()
    try:
        df = _fetch_series(series_id, observation_start=start)
    except Exception as exc:
        raise MacroDataUnavailableError(series_id, str(exc)) from exc
    rows = df.tail(n)
    return [(r.date.date(), r.value) for _, r in rows.iterrows()]


def get_usd_myr_rate() -> float:
    """Return current USD/MYR exchange rate from FRED (series DEXMAUS).

    Gap D: This is the required FX feed for MYR-base cross-market scoring.
    Raises FXRateUnavailableError if FRED DEXMAUS cannot be fetched.
    """
    from incomos.core.exceptions import FXRateUnavailableError
    try:
        rate = get_latest_value("DEXMAUS", lookback_days=10)
    except Exception as exc:
        raise FXRateUnavailableError() from exc
    logger.info("USD/MYR rate: %.4f (FRED DEXMAUS)", rate)
    return rate

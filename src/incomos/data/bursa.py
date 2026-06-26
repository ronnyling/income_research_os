"""Bursa Malaysia stock data client — V1 implementation using yfinance.

V1 limitations (architecture Gap E — documented, not silent):
  - sector_override_eligible = False for ALL MY stocks.
    Malaysian sector peer baskets are less reliable than US ETF proxies.
    This is a HARD V1 rule; do not override without explicit architecture change.
  - Annual report PDFs are not parsed (unstructured integration is Phase 2).
  - Community sources (klsescreener, i3investor) are not integrated in V1.
  - yfinance .KL coverage varies; missing fields are expected for smaller stocks.
    data_quality = "PARTIAL" is the norm, not an error.

Data contract (Pydantic schema defined before scraper per architecture, Gap E):
  BursaStockData    — price + market snapshot
  BursaFinancials   — annual financial summary (yfinance income + cashflow)

USD/MYR conversion: all figures from yfinance for .KL stocks are already in MYR.
No conversion needed.  US stocks are handled separately in edgar.py + market.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data contracts (Gap E requirement: schema before scraper)
# ---------------------------------------------------------------------------

class BursaStockData(BaseModel):
    """Market snapshot for a Bursa Malaysia-listed stock."""

    stock_code: str                        # e.g. "1155" (Maybank)
    company_name: str
    exchange: str = "MY"
    yahoo_ticker: str                      # e.g. "1155.KL"
    current_price_myr: float
    week52_high_myr: float
    week52_low_myr: float
    pct_below_52w_high: float              # 0.0 = at high, 0.30 = 30% below
    rsi_14: float | None = None
    volume_ratio: float | None = None      # current volume / 20-day average
    market_cap_myr: float | None = None
    dividend_yield_pct: float | None = None
    pe_ratio: float | None = None
    sector: str | None = None
    # Gap C: sector_override_eligible is True when yfinance provides a sector.
    # Callers may then use the corresponding US sector ETF as a peer-breadth proxy.
    sector_override_eligible: bool = False
    data_quality: str = "PARTIAL"          # FULL | PARTIAL | INSUFFICIENT
    data_source: str = "yfinance_kl"
    last_fetched_utc: str = ""             # ISO-8601 UTC timestamp of this fetch


class BursaFinancials(BaseModel):
    """Annual financial summary for a Bursa Malaysia-listed stock.

    All monetary figures in MYR.  Missing values are None — do not zero-fill.
    FCF = operating_cash_flow_myr - capex_myr (compute at use site).
    """

    stock_code: str
    fiscal_year: int
    revenue_myr: float | None = None
    net_income_myr: float | None = None
    operating_cash_flow_myr: float | None = None
    capex_myr: float | None = None          # absolute value (positive)
    dividends_paid_myr: float | None = None  # absolute value (positive)
    dps_declared: float | None = None
    data_quality: str = "PARTIAL"
    data_source: str = "yfinance_kl"
    last_fetched_utc: str = ""             # ISO-8601 UTC timestamp of this fetch


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_yahoo_ticker(stock_code: str) -> str:
    code = stock_code.strip().upper()
    return code if code.endswith(".KL") else f"{code}.KL"


def _get_df_value(df: pd.DataFrame | None, col: pd.Timestamp, *keys: str) -> float | None:
    """Safe getter for yfinance financial DataFrame cells."""
    if df is None or df.empty:
        return None
    for key in keys:
        if key in df.index:
            try:
                val = df.loc[key, col]
                if val is not None and not pd.isna(val):
                    return float(val)
            except (KeyError, TypeError):
                continue
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_bursa_stock(stock_code: str) -> BursaStockData | None:
    """Fetch market snapshot for a Bursa Malaysia stock.

    Returns None if yfinance returns no price data (delisted or unavailable).
    All fields in MYR — no currency conversion required.
    """
    yahoo_ticker = _to_yahoo_ticker(stock_code)
    try:
        t = yf.Ticker(yahoo_ticker)
        info = t.info or {}
        hist = t.history(period="1y")

        if hist.empty:
            logger.warning("Bursa %s: no 1-year history from yfinance", stock_code)
            return None

        # Current price: prefer live quote, fall back to last close
        current_price: float = float(
            info.get("regularMarketPrice")
            or info.get("currentPrice")
            or hist["Close"].iloc[-1]
        )
        if current_price <= 0:
            logger.warning("Bursa %s: current price is zero or negative", stock_code)
            return None

        week52_high: float = float(info.get("fiftyTwoWeekHigh") or hist["Close"].max())
        week52_low: float = float(info.get("fiftyTwoWeekLow") or hist["Close"].min())
        pct_below: float = (
            (week52_high - current_price) / week52_high if week52_high > 0 else 0.0
        )

        # RSI-14
        rsi_14: float | None = None
        if len(hist) >= 15:
            delta = hist["Close"].diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss.replace(0.0, float("nan"))
            rsi_series = 100 - (100 / (1 + rs))
            if not rsi_series.empty and not pd.isna(rsi_series.iloc[-1]):
                rsi_14 = round(float(rsi_series.iloc[-1]), 1)

        # Volume ratio: last close vs 20-day average
        volume_ratio: float | None = None
        if "Volume" in hist.columns and len(hist) >= 20:
            avg_vol = hist["Volume"].rolling(20).mean().iloc[-1]
            if avg_vol and avg_vol > 0:
                volume_ratio = round(float(hist["Volume"].iloc[-1]) / float(avg_vol), 2)

        # Data quality grading:
        # FULL    = price + div yield + market cap + sector all present
        # PARTIAL = price present but at least one of the above missing
        # INSUFFICIENT = would be returned as None before this point
        has_core = bool(info.get("marketCap") and info.get("trailingAnnualDividendYield"))
        has_sector = bool(info.get("sector"))
        quality = "FULL" if (has_core and has_sector) else "PARTIAL"

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        return BursaStockData(
            stock_code=stock_code,
            company_name=info.get("longName") or info.get("shortName") or stock_code,
            yahoo_ticker=yahoo_ticker,
            current_price_myr=round(current_price, 4),
            week52_high_myr=round(week52_high, 4),
            week52_low_myr=round(week52_low, 4),
            pct_below_52w_high=round(pct_below, 4),
            rsi_14=rsi_14,
            volume_ratio=volume_ratio,
            market_cap_myr=float(info["marketCap"]) if info.get("marketCap") else None,
            dividend_yield_pct=(
                round(float(info["trailingAnnualDividendYield"]) * 100, 2)
                if info.get("trailingAnnualDividendYield")
                else None
            ),
            pe_ratio=float(info["trailingPE"]) if info.get("trailingPE") else None,
            sector=info.get("sector"),
            # Gap C: sector_override_eligible when sector is available.
            # Callers can use the US sector ETF as a peer-breadth proxy.
            sector_override_eligible=has_sector,
            data_quality=quality,
            last_fetched_utc=now_utc,
        )

    except Exception as exc:
        logger.error("Bursa %s: get_bursa_stock failed: %s", stock_code, exc)
        return None


def get_bursa_financials(stock_code: str, years: int = 5) -> list[BursaFinancials]:
    """Fetch annual financials for a Bursa Malaysia stock.

    Returns a list ordered most-recent-first.  May be empty if yfinance has no
    financial data (common for smaller Bursa stocks).
    All figures in MYR — no conversion.
    """
    yahoo_ticker = _to_yahoo_ticker(stock_code)
    results: list[BursaFinancials] = []

    try:
        t = yf.Ticker(yahoo_ticker)
        income = t.financials   # rows = line items, cols = fiscal year dates
        cashflow = t.cashflow

        if income is None or income.empty:
            logger.warning("Bursa %s: no financial statements from yfinance", stock_code)
            return []

        for col in list(income.columns)[:years]:
            fy = col.year

            revenue = _get_df_value(income, col, "Total Revenue", "Revenue")
            net_income = _get_df_value(income, col, "Net Income", "Net Income Common Stockholders")
            op_cf = _get_df_value(cashflow, col,
                                  "Operating Cash Flow",
                                  "Total Cash From Operating Activities",
                                  "Net Cash Provided By Operating Activities")
            capex_raw = _get_df_value(cashflow, col,
                                      "Capital Expenditure",
                                      "Capital Expenditures",
                                      "Purchase Of Property Plant And Equipment")
            div_raw = _get_df_value(cashflow, col,
                                    "Common Stock Dividend Paid",
                                    "Dividends Paid",
                                    "Payment Of Dividends")

            results.append(BursaFinancials(
                stock_code=stock_code,
                fiscal_year=fy,
                revenue_myr=revenue,
                net_income_myr=net_income,
                operating_cash_flow_myr=op_cf,
                capex_myr=abs(capex_raw) if capex_raw is not None else None,
                dividends_paid_myr=abs(div_raw) if div_raw is not None else None,
                data_quality="PARTIAL",
                last_fetched_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ))

    except Exception as exc:
        logger.error("Bursa %s: get_bursa_financials failed: %s", stock_code, exc)

    return results

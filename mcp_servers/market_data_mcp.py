"""Market Data MCP Server.

Exposes price snapshots, OHLCV, and USD/MYR FX rate as MCP tools.
USD/MYR source: FRED DEXMAUS series — never a hardcoded fallback (Gap D rule).

Run:
    python mcp_servers/market_data_mcp.py

Tools:
  get_price_snapshot  — RSI, 52W high, volume ratio for any ticker
  get_usd_myr_rate    — live USD/MYR rate from FRED DEXMAUS
  get_ohlcv           — raw OHLCV history via yfinance
"""

from __future__ import annotations

import sys
import os

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_repo_root, "src"))

from mcp.server.fastmcp import FastMCP

from incomos.data.market import get_price_snapshot
from incomos.data.fred import get_usd_myr_rate as _get_usd_myr
import yfinance as yf

mcp_server = FastMCP(
    "market-data",
    instructions="Price snapshots, OHLCV, and USD/MYR FX rate",
)


@mcp_server.tool()
def get_price_snapshot_tool(ticker: str) -> dict:
    """Return RSI-14, 52W high, pct below 52W high, and volume ratio for a ticker.

    Works for both US tickers and Bursa .KL tickers.
    All figures in native currency (USD for US, MYR for .KL).
    """
    try:
        snap = get_price_snapshot(ticker)
        return {
            "ticker": snap.ticker,
            "current_price": snap.current_price,
            "week52_high": snap.week52_high,
            "pct_below_52w_high": snap.pct_below_52w_high,
            "rsi_14": snap.rsi_14,
            "volume_ratio": snap.volume_ratio,
        }
    except Exception as exc:
        return {"error": str(exc), "ticker": ticker}


@mcp_server.tool()
def get_usd_myr_rate_tool() -> dict:
    """Return the current USD/MYR exchange rate from FRED DEXMAUS.

    Gap D rule: FRED is the authoritative source.  Returns None rate if FRED
    is unavailable — callers must not fall back to a hardcoded value.
    """
    rate = _get_usd_myr()
    return {
        "usd_myr": rate,
        "source": "FRED_DEXMAUS",
        "available": rate is not None,
    }


@mcp_server.tool()
def get_ohlcv(ticker: str, period: str = "1y") -> dict:
    """Return OHLCV history for a ticker.

    period: 1mo | 3mo | 6mo | 1y | 2y | 5y
    Returns {dates, opens, highs, lows, closes, volumes} as parallel lists.
    """
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if hist.empty:
            return {"error": f"No data for {ticker}", "ticker": ticker}
        return {
            "ticker": ticker,
            "period": period,
            "dates": [d.strftime("%Y-%m-%d") for d in hist.index],
            "opens": hist["Open"].round(4).tolist(),
            "highs": hist["High"].round(4).tolist(),
            "lows": hist["Low"].round(4).tolist(),
            "closes": hist["Close"].round(4).tolist(),
            "volumes": hist["Volume"].tolist(),
        }
    except Exception as exc:
        return {"error": str(exc), "ticker": ticker}


if __name__ == "__main__":
    mcp_server.run()

"""Bursa Malaysia MCP Server.

Exposes Bursa Malaysia stock data (V1: yfinance .KL) as MCP tools.

Run:
    python mcp_servers/bursa_malaysia_mcp.py

V1 limitations (documented, not silent):
  - sector_override_eligible = False for ALL results (hard rule).
  - Annual report PDFs are not parsed in V1.
  - yfinance .KL coverage varies — missing fields are expected.

Tools:
  get_bursa_stock      — market snapshot for a Bursa stock code
  get_bursa_financials — annual financials for a Bursa stock code
  screen_bursa_universe — screen a list of Bursa stocks by yield and P/E
"""

from __future__ import annotations

import sys
import os

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_repo_root, "src"))

from mcp.server.fastmcp import FastMCP

from incomos.data.bursa import get_bursa_stock, get_bursa_financials

mcp_server = FastMCP(
    "bursa-malaysia",
    instructions="Bursa Malaysia stock data — V1 via yfinance .KL (Gap E, see limitations)",
)


@mcp_server.tool()
def get_bursa_stock_tool(stock_code: str) -> dict:
    """Return market snapshot for a Bursa Malaysia stock.

    stock_code: Bursa stock code, e.g. "1155" (Maybank), "1295" (Public Bank).
    Appends .KL suffix automatically.

    Returns BursaStockData fields.  sector_override_eligible is always False
    in V1 (Malaysian sector peer baskets are not reliable — architecture rule).
    """
    data = get_bursa_stock(stock_code)
    if data is None:
        return {
            "error": f"No data for {stock_code} — stock may be delisted or yfinance gap",
            "stock_code": stock_code,
        }
    return data.model_dump()


@mcp_server.tool()
def get_bursa_financials_tool(stock_code: str, years: int = 5) -> list[dict]:
    """Return annual financials for a Bursa Malaysia stock.

    All figures in MYR.  Missing values are null — do not zero-fill.
    FCF = operating_cash_flow_myr - capex_myr (compute at use site).
    """
    records = get_bursa_financials(stock_code, years=years)
    if not records:
        return [{"error": f"No financial data for {stock_code} — common for smaller MY stocks"}]
    return [r.model_dump() for r in records]


@mcp_server.tool()
def screen_bursa_universe(
    stock_codes: list[str],
    min_yield_pct: float | None = None,
    max_pe: float | None = None,
    max_pct_below_52w: float | None = None,
) -> list[dict]:
    """Screen a list of Bursa stock codes by yield, P/E, and dip depth.

    All parameters are optional filters — omit to skip that filter.
    min_yield_pct: e.g. 4.0 for minimum 4% dividend yield
    max_pe: e.g. 20.0 to exclude overvalued stocks
    max_pct_below_52w: e.g. 0.30 — only stocks ≤30% below 52W high

    Returns filtered list of BursaStockData dicts ordered by pct_below desc.
    """
    results = []
    for code in stock_codes:
        snap = get_bursa_stock(code)
        if snap is None:
            continue

        if min_yield_pct is not None:
            if snap.dividend_yield_pct is None or snap.dividend_yield_pct < min_yield_pct:
                continue

        if max_pe is not None:
            if snap.pe_ratio is None or snap.pe_ratio > max_pe:
                continue

        if max_pct_below_52w is not None:
            if snap.pct_below_52w_high > max_pct_below_52w:
                continue

        results.append(snap.model_dump())

    results.sort(key=lambda x: x.get("pct_below_52w_high", 0), reverse=True)
    return results


if __name__ == "__main__":
    mcp_server.run()

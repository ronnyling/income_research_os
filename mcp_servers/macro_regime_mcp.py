"""Macro Regime MCP Server.

Exposes the macro regime contract and underlying FRED data as MCP tools.

Run:
    python mcp_servers/macro_regime_mcp.py

Macro contract axes (V1 states only):
  market_structure  : TRENDING_UP | TRENDING_DOWN | RANGING
  growth            : EXPANSION | RECESSION_RISK
  rates             : STABLE | RATE_SHOCK_UP | RATE_SHOCK_DOWN
  financial_cond    : LOOSE | TIGHTENING

Sources:
  NY Fed yield curve probability (public CSV)
  FRED H.15 (DGS2, DGS10)
  FRED NFCI/ANFCI
  yfinance (S&P 500 for market structure)

Tools:
  get_macro_regime     — full MacroRegimeContract
  get_yield_curve_data — raw yield curve probability value
  get_interest_rates   — DGS2, DGS10, and spread
"""

from __future__ import annotations

import sys
import os

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_repo_root, "src"))

from mcp.server.fastmcp import FastMCP

from incomos.macro.regime import detect_macro_regime
from incomos.data.fred import get_latest_value

mcp_server = FastMCP(
    "macro-regime",
    instructions="Macro regime contract and FRED data (yield curve, rates, financial conditions)",
)


@mcp_server.tool()
def get_macro_regime() -> dict:
    """Return the full MacroRegimeContract with all four axes.

    Each axis has: state, confidence.
    Confidence permission levels:
      ≥ 0.55 → reference only (no classification change)
      ≥ 0.70 → adjust weights
      ≥ 0.85 + sector_override_eligible → override eligible
    """
    try:
        regime = detect_macro_regime()
        return {
            "market_structure": {
                "state": str(regime.market_structure.state).split(".")[-1],
                "confidence": regime.market_structure.confidence,
            },
            "growth": {
                "state": str(regime.growth.state).split(".")[-1],
                "confidence": regime.growth.confidence,
            },
            "rates": {
                "state": str(regime.rates.state).split(".")[-1],
                "confidence": regime.rates.confidence,
            },
            "financial_conditions": {
                "state": str(regime.financial_conditions.state).split(".")[-1],
                "confidence": regime.financial_conditions.confidence,
            },
        }
    except Exception as exc:
        return {"error": str(exc)}


@mcp_server.tool()
def get_yield_curve_data() -> dict:
    """Return the current NY Fed yield curve recession probability.

    Source: public CSV from NY Fed (no API key required).
    Values > 0.30 → RECESSION_RISK state.
    """
    try:
        # T10Y2Y spread as a proxy (NY Fed CSV is fetched inside regime.py)
        dgs2 = get_latest_value("DGS2")
        dgs10 = get_latest_value("DGS10")
        spread = (dgs10 - dgs2) if dgs2 and dgs10 else None
        return {
            "dgs2": dgs2,
            "dgs10": dgs10,
            "spread_10y_2y": spread,
            "source": "FRED_H15",
        }
    except Exception as exc:
        return {"error": str(exc)}


@mcp_server.tool()
def get_interest_rates() -> dict:
    """Return FRED H.15 Treasury rates: DGS2, DGS10, and T10Y2Y spread."""
    try:
        dgs2 = get_latest_value("DGS2")
        dgs10 = get_latest_value("DGS10")
        nfci = get_latest_value("NFCI")
        anfci = get_latest_value("ANFCI")
        return {
            "dgs2": dgs2,
            "dgs10": dgs10,
            "spread_10y_2y": (dgs10 - dgs2) if dgs2 and dgs10 else None,
            "nfci": nfci,
            "anfci": anfci,
            "source": "FRED",
        }
    except Exception as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    mcp_server.run()

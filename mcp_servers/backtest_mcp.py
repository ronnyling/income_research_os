"""Backtest / Forward-Return Validation MCP Server.

Exposes score calibration and forward-return computation as MCP tools.

Run:
    python mcp_servers/backtest_mcp.py

Gap B reminder:
  The forward-return success threshold is NOT defined by the architecture.
  All threshold parameters are caller-supplied.  This server never defaults
  a success cutoff — do not add one.

Tools:
  compute_forward_return  — actual returns at 30/90/180/365d from an entry
  calibration_report_csv  — score calibration from a CSV of scored entries
"""

from __future__ import annotations

import sys
import os

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_repo_root, "src"))

from mcp.server.fastmcp import FastMCP

from incomos.backtest.validation import ForwardReturnValidator

mcp_server = FastMCP(
    "backtest",
    instructions="Forward-return validation and score calibration (Gap B)",
)

_validator = ForwardReturnValidator()


@mcp_server.tool()
def compute_forward_return(
    ticker: str,
    entry_date: str,
    entry_price: float,
    horizons: list[int] | None = None,
    exchange: str = "US",
) -> list[dict]:
    """Compute actual price returns at each horizon from an entry point.

    entry_date: YYYY-MM-DD
    horizons: list of days, default [30, 90, 180, 365]
    exchange: US | MY (affects benchmark: ^GSPC vs ^KLSE)

    Returns one record per horizon.  Horizons that have not yet elapsed return
    null for exit_price and return_pct.
    """
    if horizons is None:
        horizons = [30, 90, 180, 365]

    results = _validator.compute_forward_return(
        ticker=ticker,
        entry_date=entry_date,
        entry_price=entry_price,
        horizons=horizons,
        exchange=exchange,
    )
    return [r.__dict__ for r in results]


@mcp_server.tool()
def calibration_report_csv(
    csv_path: str,
    horizon_days: int = 90,
    threshold: float | None = None,
) -> dict:
    """Run score calibration from a CSV file of historical scored entries.

    CSV must have columns: ticker, entry_date, entry_price, opportunity_score, exchange

    threshold (Gap B):
      Return threshold for hit_rate calculation (e.g. 0.05 = 5%).
      If not provided, hit_rate will be null in the report.
      Do NOT supply a default — the architecture has not defined this threshold.

    Returns the calibration report as a dict.
    """
    try:
        import pandas as pd
        df = pd.read_csv(csv_path)
    except Exception as exc:
        return {"error": f"Failed to read CSV: {exc}"}

    try:
        report = _validator.calibration_report(
            historical_df=df,
            horizon_days=horizon_days,
            threshold=threshold,
        )
        return {
            "generated_at": report.generated_at,
            "horizon_days": report.horizon_days,
            "n_total": report.n_total,
            "notes": report.notes,
            "bucket_stats": [b.__dict__ for b in report.bucket_stats],
        }
    except Exception as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    mcp_server.run()

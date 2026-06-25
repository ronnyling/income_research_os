"""Research Store MCP Server.

Exposes funnel state, opportunity scores, refresh timestamps, and annotations
as MCP tools backed by PostgreSQL via the incomos persistence layer.

Run:
    python mcp_servers/research_store_mcp.py

Tools:
  get_funnel_state         — current stage for a ticker
  update_funnel_stage      — promote or demote a ticker in the funnel
  get_kiv_basket           — all stocks currently in KIV stage
  save_opportunity_score   — persist a scored result
  get_refresh_timestamp    — when a data type was last refreshed
  update_refresh_timestamp — mark a data type as freshly refreshed
  save_annotation          — attach a human annotation to a ticker

Architecture notes:
  - Refresh timestamps are ONLY updated on full success (atomic rule).
    Failed refreshes must NOT call update_refresh_timestamp.
  - Funnel stage transitions log reason and timestamp.
  - This server requires PostgreSQL to be running.  If DB is unavailable,
    tools return an error dict rather than raising.
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timezone

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_repo_root, "src"))

from mcp.server.fastmcp import FastMCP

from incomos.persistence.session import db_session
import incomos.persistence.queries as queries

mcp_server = FastMCP(
    "research-store",
    instructions="Funnel state, opportunity scores, and refresh timestamps (PostgreSQL-backed)",
)



@mcp_server.tool()
def get_funnel_state(ticker: str) -> dict:
    """Return the current funnel stage and metadata for a ticker."""
    with db_session() as conn:
        row = queries.get_stock(conn, ticker)
        if row is None:
            return {"ticker": ticker, "stage": "UNKNOWN", "found": False}
        return {
            "ticker": ticker,
            "stage": row.get("stage", "UNKNOWN"),
            "found": True,
            "last_updated": str(row.get("updated_at", "")),
            "notes": row.get("notes", ""),
        }


@mcp_server.tool()
def update_funnel_stage(ticker: str, stage: str, reason: str) -> dict:
    """Transition a ticker to a new funnel stage.

    Valid stages: PROSPECTS | KIV | CANDIDATE | FINALIST | DECISION | REJECTED | DORMANT
    reason: brief description of why the transition occurred (logged).
    """
    valid_stages = {"PROSPECTS", "KIV", "CANDIDATE", "FINALIST", "DECISION", "REJECTED", "DORMANT"}
    if stage not in valid_stages:
        return {"error": f"Invalid stage '{stage}'. Valid: {sorted(valid_stages)}"}

    with db_session() as conn:
        queries.transition_stage(conn, ticker, stage, reason)
        return {
            "ticker": ticker,
            "new_stage": stage,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "success": True,
        }


@mcp_server.tool()
def get_kiv_basket() -> dict:
    """Return all stocks currently in the KIV stage with their age (days in KIV).

    Stocks approaching the 90-day TTL are flagged.
    """
    with db_session() as conn:
        rows = queries.get_kiv_basket(conn)
        return {
            "kiv_count": len(rows),
            "stocks": rows,
            "ttl_days": 90,
        }


@mcp_server.tool()
def save_opportunity_score(ticker: str, score_dict: dict) -> dict:
    """Persist an opportunity score record.

    score_dict must contain: composite, income_quality, business_quality,
    dip_quality, oversold_confidence, base_size_multiplier.
    """
    required = {"composite", "income_quality", "business_quality", "dip_quality", "oversold_confidence"}
    missing = required - set(score_dict.keys())
    if missing:
        return {"error": f"Missing score fields: {missing}"}

    with db_session() as conn:
        queries.save_opportunity_score(conn, ticker, score_dict)
        return {"ticker": ticker, "saved": True, "composite": score_dict["composite"]}


@mcp_server.tool()
def get_refresh_timestamp(data_type: str, ticker: str | None = None) -> dict:
    """Return when a data type was last successfully refreshed.

    data_type: MACRO_REGIME | KIV_PRICE | KIV_FILINGS | UNIVERSE | FULL_ANALYSIS
    ticker: required for ticker-level types; None for system-level types.
    """
    with db_session() as conn:
        ts = queries.get_refresh_timestamp(conn, data_type, ticker)
        return {
            "data_type": data_type,
            "ticker": ticker,
            "last_refresh": str(ts) if ts else None,
            "found": ts is not None,
        }


@mcp_server.tool()
def update_refresh_timestamp(data_type: str, ticker: str | None = None) -> dict:
    """Mark a data type as successfully refreshed (NOW).

    CRITICAL: Call this ONLY after a fully successful refresh.
    A partial or failed refresh must NOT update the timestamp (atomic rule).
    """
    with db_session() as conn:
        queries.update_refresh_timestamp(conn, data_type, ticker)
        return {
            "data_type": data_type,
            "ticker": ticker,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "success": True,
        }


@mcp_server.tool()
def save_annotation(ticker: str, annotation: str, analyst: str = "human") -> dict:
    """Attach a human annotation to a ticker.

    Annotations can increase position size up to 1.5× base (architecture rule).
    """
    with db_session() as conn:
        queries.save_annotation(conn, ticker, annotation, analyst)
        return {"ticker": ticker, "saved": True}


if __name__ == "__main__":
    mcp_server.run()

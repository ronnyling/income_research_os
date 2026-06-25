"""SEC Filings MCP Server.

Exposes EDGAR XBRL metrics and full-text filing sections as MCP tools.
All operations are rule-based — no LLM calls.

Run:
    python mcp_servers/sec_filings_mcp.py

Tools:
  get_xbrl_metrics       — annual XBRL financial metrics for a US ticker
  get_filing_sections    — extracted 10-K sections (RISK_FACTORS, MDAA)
  get_yoy_sections       — current + prior year 10-K sections side by side
  get_8k_events          — recent 8-K event metadata
"""

from __future__ import annotations

import sys
import os

# Ensure src/ is on path when run as a standalone process
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_repo_root, "src"))

from mcp.server.fastmcp import FastMCP

from incomos.data.edgar import EdgarClient
from incomos.data.filings import FilingsClient

mcp_server = FastMCP(
    "sec-filings",
    instructions="EDGAR XBRL metrics and full-text filing sections for US stocks",
)

_edgar = EdgarClient()
_filings = FilingsClient()


@mcp_server.tool()
def get_xbrl_metrics(ticker: str, years: int = 5) -> list[dict]:
    """Return annual XBRL financial metrics for a US-listed ticker.

    Pulls FCF, dividends, revenue, net income from EDGAR company facts API.
    Returns a list of yearly records ordered most-recent-first.
    """
    cik = _edgar.resolve_cik(ticker)
    if not cik:
        return [{"error": f"CIK not found for {ticker}"}]
    metrics = _edgar.get_annual_metrics(ticker, cik, years=years)
    return [m.__dict__ if hasattr(m, "__dict__") else m for m in metrics]


@mcp_server.tool()
def get_filing_sections(ticker: str, section: str = "RISK_FACTORS", filing_index: int = 0) -> dict:
    """Extract a named section from a 10-K filing.

    section: RISK_FACTORS | MDAA
    filing_index: 0 = most recent, 1 = prior year

    Returns: {section_name, text, filing_date, word_count, truncated, error?}
    """
    cik = _edgar.resolve_cik(ticker)
    if not cik:
        return {"error": f"CIK not found for {ticker}"}

    sections = _filings.get_filing_sections(
        ticker, cik, "10-K", (section,), filing_index=filing_index
    )
    if not sections or section not in sections:
        return {"error": f"Section {section} not found in filing (index={filing_index})"}

    fs = sections[section]
    return {
        "ticker": fs.ticker,
        "section_name": fs.section_name,
        "filing_date": fs.filing_date,
        "filing_index": filing_index,
        "word_count": fs.word_count,
        "truncated": fs.truncated,
        "text": fs.text,
    }


@mcp_server.tool()
def get_yoy_sections(ticker: str, section: str = "RISK_FACTORS") -> dict:
    """Fetch the same section from the current AND prior year 10-K.

    This is the primary input for MiMo's YoY risk-factor diff — the
    highest-value filing analysis use case.

    Returns: {current: {...}, prior: {...}}
    """
    cik = _edgar.resolve_cik(ticker)
    if not cik:
        return {"error": f"CIK not found for {ticker}"}

    current_map, prior_map = _filings.get_yoy_sections(ticker, cik, sections=(section,))

    def _format(m: dict, label: str) -> dict:
        if not m or section not in m:
            return {"available": False, "label": label}
        fs = m[section]
        return {
            "available": True,
            "label": label,
            "filing_date": fs.filing_date,
            "word_count": fs.word_count,
            "truncated": fs.truncated,
            "text": fs.text,
        }

    return {
        "ticker": ticker,
        "section": section,
        "current": _format(current_map, "current"),
        "prior": _format(prior_map, "prior"),
    }


@mcp_server.tool()
def get_8k_events(ticker: str, limit: int = 5) -> list[dict]:
    """Return metadata for recent 8-K filings.

    Used to detect KIV demotion triggers: dividend suspension, restatement,
    going-concern warnings.  Returns filing metadata only — caller must fetch
    and classify the content.
    """
    cik = _edgar.resolve_cik(ticker)
    if not cik:
        return [{"error": f"CIK not found for {ticker}"}]
    return _filings.get_recent_8k_events(ticker, cik, limit=limit)


if __name__ == "__main__":
    mcp_server.run()

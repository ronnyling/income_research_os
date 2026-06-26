"""Dynamic dividend stock universe from NOBL ETF holdings.

Fetches the current S&P 500 Dividend Aristocrats from the ProShares NOBL ETF
holdings page. Falls back to the static list in universe.py if the fetch fails.

Architecture: This module is a data source for universe.py's get_universe().
It does NOT replace universe.py — it extends it with a "nobl" tier that
pulls live holdings instead of a hardcoded list.

Mode 3 fix: The static universe goes stale as Aristocrats are added/removed
annually. NOBL holdings are the canonical source and update monthly.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# ProShares NOBL holdings CSV — public, no API key required.
# This URL may change; if it does, the module falls back gracefully.
_NOBL_HOLDINGS_URL = "https://www.proshares.com/funds/nobl_holdings.csv"
_REQUEST_TIMEOUT = 15
_USER_AGENT = "IncomOS research@incomos.local"


def fetch_nobl_holdings() -> list[str] | None:
    """Fetch current NOBL ETF holdings (S&P 500 Dividend Aristocrats).

    Returns:
        List of ticker symbols, or None if fetch fails.
        Returns None (not empty list) so callers can distinguish between
        "fetch failed" and "ETF has no holdings" (which shouldn't happen).
    """
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            resp = client.get(
                _NOBL_HOLDINGS_URL,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("NOBL holdings fetch failed: %s", exc)
        return None

    text = resp.text
    if not text.strip():
        logger.warning("NOBL holdings response was empty")
        return None

    return _parse_nobl_csv(text)


def _parse_nobl_csv(text: str) -> list[str] | None:
    """Parse the NOBL holdings CSV and extract ticker symbols.

    ProShares CSV format has a header row, then data rows. The ticker column
    is typically named "Ticker" or "Symbol". Some rows may be cash/other
    holdings that don't have a ticker — these are filtered out.
    """
    try:
        # ProShares CSV may have a preamble before the actual header
        # Look for the header row by finding a line with "Ticker" or "Symbol"
        lines = text.strip().split("\n")
        header_idx = 0
        for i, line in enumerate(lines):
            lower = line.lower()
            if "ticker" in lower or "symbol" in lower or "name" in lower:
                header_idx = i
                break

        csv_text = "\n".join(lines[header_idx:])
        reader = csv.DictReader(io.StringIO(csv_text))

        # Find the ticker column (case-insensitive)
        fieldnames = reader.fieldnames or []
        ticker_col = None
        for fn in fieldnames:
            if fn and fn.lower() in ("ticker", "symbol", "stock ticker"):
                ticker_col = fn
                break

        if ticker_col is None:
            # Try first column as fallback
            if fieldnames:
                ticker_col = fieldnames[0]
                logger.warning("NOBL CSV: no 'Ticker' column found, using first column '%s'", ticker_col)
            else:
                logger.warning("NOBL CSV: no columns found")
                return None

        tickers: list[str] = []
        for row in reader:
            raw = row.get(ticker_col, "").strip()
            if not raw:
                continue
            # Clean ticker: remove exchange suffixes, whitespace, special chars
            ticker = raw.split()[0].upper().strip()
            # Skip non-equity rows (cash, futures, etc.)
            if ticker and re.match(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$', ticker):
                # Remove .US suffix if present
                ticker = ticker.replace(".US", "")
                tickers.append(ticker)

        if not tickers:
            logger.warning("NOBL CSV: parsed successfully but found 0 tickers")
            return None

        # Deduplicate preserving order
        seen: set[str] = set()
        result: list[str] = []
        for t in tickers:
            if t not in seen:
                seen.add(t)
                result.append(t)

        logger.info("NOBL holdings: fetched %d unique tickers", len(result))
        return result

    except Exception as exc:
        logger.warning("NOBL CSV parsing failed: %s", exc)
        return None

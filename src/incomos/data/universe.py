"""Stock universe module — provides the candidate stock list for funnel screening.

V1 uses a curated static list of US dividend-paying stocks (Dividend Aristocrats,
Dividend Kings, and quality income payers). This is a starting point — the
architecture targets 200-600 stocks from screener APIs or ETF holdings.

The list is organized by tier:
  TIER_1_ARISTOCRATS: S&P 500 Dividend Aristocrats (25+ years of dividend growth)
  TIER_2_KINGS: Dividend Kings (50+ years of dividend growth)
  TIER_3_QUALITY: Quality dividend payers with strong FCF and growth
  NOBL_DYNAMIC: Live holdings from ProShares NOBL ETF (fetched at runtime)

All tickers are US-listed. The system processes these through Stage 0→1
quality screen — not all will pass (some may have deteriorated).

Mode 3 fix: The static universe goes stale as Aristocrats are added/removed
annually. NOBL holdings are the canonical source and update monthly.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# S&P 500 Dividend Aristocrats — 25+ consecutive years of dividend increases
# Curated subset focused on sectors with strong income characteristics
TIER_1_ARISTOCRATS: list[str] = [
    # Consumer Staples
    "KO", "PG", "PEP", "CL", "CLX", "CHD", "SYY", "ADM", "TSN", "CAG",
    "HRL", "MKC", "SJM", "CPB", "BG", "KDP", "MDLZ", "KMB", "GIS",
    # Healthcare
    "JNJ", "ABT", "MDT", "BDX", "BAX", "BMY", "PFE", "MRK", "AMGN",
    "SYK", "ZBH", "ALGN", "EW", "ISRG",
    # Industrials
    "MMM", "EMR", "ITW", "SWK", "DOV", "NUE", "PH", "GPC", "FAST",
    "APD", "SHW", "PPG", "MMM", "GD", "LMT", "RTX", "CAT", "DE",
    # Utilities
    "NEE", "DUK", "SO", "D", "AEP", "SRE", "XEL", "WEC", "ES", "ED",
    "AWK", "ATO", "CMS", "CNP", "NI", "PNW",
    # Consumer Discretionary
    "MCD", "TGT", "LOW", "TJX", "ROST", "NKE", "SBUX", "YUM",
    # Financials
    "JPM", "BLK", "TROW", "AFL", "CB", "MMC", "TRV", "BEN", "FDS",
    "ICE", "CME", "NTRS", "STT",
    # Technology
    "MSFT", "ACN", "ADP", "INTC", "TXN", "IBM", "APH", "GLW", "HPQ",
    # Real Estate
    "O", "WPC", "FRT", "PEB", "SPG", "REG",
    # Materials
    "LIN", "APD", "ALB", "EMN", "SEE",
    # Communication
    "VZ", "T", "CMCSA", "DIS",
]


# Dividend Kings — 50+ consecutive years of dividend increases
# Overlaps with Aristocrats but includes some unique names
TIER_2_KINGS: list[str] = [
    "ABT", "JNJ", "KO", "PG", "MMM", "EMR", "CL", "ITW", "DOV",
    "GPC", "PH", "SWK", "LOW", "TGT", "FRT", "NUE", "AWK", "AOS",
    "HRL", "SJW", "UVV", "CWT", "NWN", "MGEE", "FMCB",
]


# Quality dividend payers — strong FCF, growing dividend, good yield
# Not necessarily Aristocrats but meet income compounder criteria
TIER_3_QUALITY: list[str] = [
    # Tech with growing dividends
    "AAPL", "AVGO", "QCOM", "ADI", "AMAT", "LRCX", "KLAC", "MCHP",
    "SNPS", "CDNS", "ANSS", "FTNT", "PANW", "CRWD",
    # Healthcare with yield
    "ABBV", "GILD", "CVS", "CI", "UNH", "HUM", "ANTM",
    # Financials with yield
    "BAC", "WFC", "GS", "MS", "AXP", "SCHW", "USB", "PNC", "TFC",
    "MTB", "FITB", "KEY", "HBAN", "CFG", "RF",
    # Energy with yield
    "XOM", "CVX", "COP", "EOG", "SLB", "OXY", "MPC", "PSX", "VLO",
    "HES", "DVN", "FANG", "HAL", "BKR",
    # REITs with yield
    "AMT", "CCI", "EQIX", "DLR", "PSA", "EXR", "MAA", "UDR", "EQR",
    "ESS", "ARE", "BXP", "SLG", "HIW", "KIM", "MAC",
    # Telecom
    "TMUS",
    # Consumer
    "MO", "PM", "STZ", "DEO", "BF-B", "CL",
    # Industrial
    "UNP", "CSX", "NSC", "FDX", "UPS", "WM", "RSG", "WSO",
]


def get_universe(tier: str = "all") -> list[str]:
    """Get the stock universe for funnel screening.

    Args:
        tier: "aristocrats", "kings", "quality", "nobl", or "all" (default)

    Returns:
        Deduplicated list of ticker symbols.

    Notes:
        "nobl" tier fetches live holdings from ProShares NOBL ETF. Falls back
        to static aristocrats list if the fetch fails.
        "all" combines static tiers (aristocrats + kings + quality).
    """
    if tier == "aristocrats":
        return list(dict.fromkeys(TIER_1_ARISTOCRATS))
    elif tier == "kings":
        return list(dict.fromkeys(TIER_2_KINGS))
    elif tier == "quality":
        return list(dict.fromkeys(TIER_3_QUALITY))
    elif tier == "nobl":
        return _get_nobl_universe()
    elif tier == "all":
        # Combine all tiers, deduplicate preserving order (aristocrats first)
        seen: set[str] = set()
        result: list[str] = []
        for ticker in TIER_1_ARISTOCRATS + TIER_2_KINGS + TIER_3_QUALITY:
            if ticker not in seen:
                seen.add(ticker)
                result.append(ticker)
        return result
    else:
        raise ValueError(f"Unknown tier: {tier}. Use 'aristocrats', 'kings', 'quality', 'nobl', or 'all'.")


def _get_nobl_universe() -> list[str]:
    """Get NOBL ETF holdings with graceful fallback to static list."""
    try:
        from incomos.data.nobl import fetch_nobl_holdings
        holdings = fetch_nobl_holdings()
        if holdings and len(holdings) > 20:
            logger.info("NOBL dynamic universe: %d tickers", len(holdings))
            return holdings
        logger.warning("NOBL fetch returned %d tickers (expected 50+), falling back to static list",
                       len(holdings) if holdings else 0)
    except Exception as exc:
        logger.warning("NOBL fetch failed (%s), falling back to static list", exc)
    return list(dict.fromkeys(TIER_1_ARISTOCRATS))


def get_universe_count() -> dict[str, int]:
    """Get the count of stocks in each tier."""
    return {
        "aristocrats": len(dict.fromkeys(TIER_1_ARISTOCRATS)),
        "kings": len(dict.fromkeys(TIER_2_KINGS)),
        "quality": len(dict.fromkeys(TIER_3_QUALITY)),
        "all": len(get_universe("all")),
    }

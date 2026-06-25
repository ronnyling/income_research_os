"""EDGAR data client — rule-based, no LLM.

Responsibilities:
  - Resolve ticker → CIK using EDGAR's public company_tickers.json
  - Fetch XBRL company facts (free, no API key)
  - Extract annual time series for key financial metrics
  - Return typed XBRLMetrics objects

Rate limit: EDGAR enforces 10 req/s max. We default to 5 req/s (configurable).
All monetary values are in USD (EDGAR native). MYR conversion happens at scoring time.

EDGAR API references (no API key required):
  Company facts: https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
  Submissions:   https://data.sec.gov/submissions/CIK{cik}.json
  Ticker map:    https://www.sec.gov/files/company_tickers.json
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from incomos.core.config import get_settings
from incomos.core.types import XBRLMetrics

logger = logging.getLogger(__name__)

_EDGAR_BASE = "https://data.sec.gov"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Cache path for the ticker → CIK map (refreshed when stale)
_TICKER_CACHE_PATH = Path("data/edgar_tickers.json")

# XBRL tags to attempt, in preference order, for each metric.
# EDGAR companies use different tags — we try them in order and take the first hit.
_XBRL_TAG_MAP: dict[str, list[str]] = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "NetRevenues",
        "TotalRevenues",
        "RevenueNet",
        "SalesRevenue",
        "RevenueFromContractWithCustomer",
    ],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
        "CapitalExpendituresIncurredButNotYetPaid",
    ],
    "dividends_paid": [
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfDividends",
        "PaymentsOfOrdinaryDividends",
        "DividendsCommonStockCash",       # used by some large-caps (e.g. Accenture)
        "DividendsCashPaid",
        "PaymentsOfDividendsCommonStockAndPreferredStock",
    ],
    "dps_declared": [
        "CommonStockDividendsPerShareDeclared",
        "DividendsCommonStockPerShareDeclared",
    ],
    "dps_paid": [
        "CommonStockDividendsPerShareCashPaid",
        "DividendsCommonStockPerShareCashPaid",
    ],
    "total_debt": [
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "DebtCurrent",
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsAndShortTermInvestments",
    ],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityAttributableToParent",
    ],
}


class EdgarClient:
    """Synchronous EDGAR data client.

    Usage:
        client = EdgarClient()
        cik = client.resolve_cik("ACN")
        metrics = client.get_annual_metrics("ACN", cik, years=5)
    """

    def __init__(self) -> None:
        cfg = get_settings()
        self._rate_limit = cfg.edgar_requests_per_second
        self._last_request_at: float = 0.0
        self._ticker_map: dict[str, str] | None = None  # ticker → CIK (zero-padded 10 digits)
        headers = {
            "User-Agent": "IncomOS Research Bot contact@incomos.local",
            "Accept-Encoding": "gzip, deflate",
        }
        self._http = httpx.Client(
            headers=headers,
            timeout=30.0,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Ticker → CIK resolution
    # ------------------------------------------------------------------

    def _load_ticker_map(self, force_refresh: bool = False) -> dict[str, str]:
        """Load (or refresh) the full EDGAR ticker → CIK map.

        Result is cached to data/edgar_tickers.json and reused across runs.
        Force refresh if the file is missing or explicitly requested.
        """
        if self._ticker_map is not None and not force_refresh:
            return self._ticker_map

        if _TICKER_CACHE_PATH.exists() and not force_refresh:
            logger.debug("Loading EDGAR ticker map from cache: %s", _TICKER_CACHE_PATH)
            with open(_TICKER_CACHE_PATH) as f:
                self._ticker_map = json.load(f)
            return self._ticker_map

        logger.info("Fetching EDGAR ticker map from SEC...")
        raw = self._get(_TICKERS_URL)
        # raw is {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
        mapping: dict[str, str] = {}
        for entry in raw.values():
            ticker = entry["ticker"].upper()
            cik = str(entry["cik_str"]).zfill(10)
            mapping[ticker] = cik

        _TICKER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_TICKER_CACHE_PATH, "w") as f:
            json.dump(mapping, f)

        self._ticker_map = mapping
        logger.info("Loaded %d tickers from EDGAR.", len(mapping))
        return self._ticker_map

    def resolve_cik(self, ticker: str) -> str:
        """Resolve a ticker to its 10-digit zero-padded CIK string.

        Raises ValueError if ticker is not found in EDGAR.
        """
        mapping = self._load_ticker_map()
        upper = ticker.upper()
        if upper not in mapping:
            # Try refreshing once in case it's a recent addition
            mapping = self._load_ticker_map(force_refresh=True)
        if upper not in mapping:
            raise ValueError(f"Ticker '{ticker}' not found in EDGAR. Not a US-listed stock?")
        return mapping[upper]

    # ------------------------------------------------------------------
    # XBRL company facts
    # ------------------------------------------------------------------

    def get_company_facts(self, cik: str) -> dict[str, Any]:
        """Fetch the full XBRL company facts JSON from EDGAR.

        Returns the 'facts' dict keyed by taxonomy (us-gaap, dei, etc.).
        """
        url = f"{_EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
        data = self._get(url)
        return data

    def get_company_info(self, cik: str) -> dict[str, Any]:
        """Fetch the submissions JSON for company name and recent filing list."""
        url = f"{_EDGAR_BASE}/submissions/CIK{cik}.json"
        return self._get(url)

    # ------------------------------------------------------------------
    # Metric extraction
    # ------------------------------------------------------------------

    def _extract_annual_series(
        self,
        facts: dict[str, Any],
        metric: str,
    ) -> list[tuple[int, float]]:
        """Extract annual (FY) values for a metric from company facts.

        Returns a list of (fiscal_year, value) tuples sorted ascending by year.
        Tries each candidate tag in _XBRL_TAG_MAP[metric] and takes the first hit.

        Values from 10-K annual filings only (fp == "FY", form == "10-K").
        """
        tags = _XBRL_TAG_MAP.get(metric, [])
        gaap = facts.get("facts", {}).get("us-gaap", {})

        for tag in tags:
            tag_data = gaap.get(tag)
            if not tag_data:
                continue
            units = tag_data.get("units", {})
            # Most financial metrics are in USD
            entries = units.get("USD") or units.get("shares") or []
            annual = [
                e for e in entries
                if e.get("fp") == "FY" and e.get("form") in ("10-K", "10-K/A")
            ]
            if not annual:
                continue

            # Deduplicate: if same fiscal_year appears multiple times (amendments),
            # take the most recently filed entry
            by_year: dict[int, dict] = {}
            for e in annual:
                fy = e.get("fy")
                if fy is None:
                    continue
                existing = by_year.get(fy)
                if existing is None or e.get("filed", "") > existing.get("filed", ""):
                    by_year[fy] = e

            result = sorted(
                [(fy, entry["val"]) for fy, entry in by_year.items()],
                key=lambda x: x[0],
            )
            logger.debug("  %s → tag '%s': %d annual entries", metric, tag, len(result))
            return result

        logger.debug("  %s → no matching XBRL tag found (tried: %s)", metric, tags)
        return []

    def get_annual_metrics(
        self,
        ticker: str,
        cik: str,
        years: int = 5,
    ) -> list[XBRLMetrics]:
        """Extract the most recent N years of annual XBRL metrics.

        Returns a list of XBRLMetrics, one per fiscal year, sorted oldest first.
        """
        logger.info("Fetching XBRL company facts for %s (CIK %s)...", ticker, cik)
        facts = self.get_company_facts(cik)
        entity_name = facts.get("entityName", ticker)

        # Extract all series
        series: dict[str, list[tuple[int, float]]] = {}
        for metric in _XBRL_TAG_MAP:
            series[metric] = self._extract_annual_series(facts, metric)

        # Determine the set of fiscal years we have data for (union across all metrics)
        all_years: set[int] = set()
        for entries in series.values():
            all_years.update(fy for fy, _ in entries)

        if not all_years:
            logger.warning("No annual XBRL data found for %s", ticker)
            return []

        recent_years = sorted(all_years)[-years:]

        result: list[XBRLMetrics] = []
        for fy in recent_years:

            def _get(metric: str) -> float | None:
                entries_dict = dict(series.get(metric, []))
                return entries_dict.get(fy)

            capex_raw = _get("capex")
            # CapEx in XBRL is usually reported as a positive outflow.
            # Ensure it is positive (some companies report as negative).
            capex = abs(capex_raw) if capex_raw is not None else None

            dividends_paid_raw = _get("dividends_paid")
            dividends_paid = abs(dividends_paid_raw) if dividends_paid_raw is not None else None

            m = XBRLMetrics(
                ticker=ticker,
                cik=cik,
                fiscal_year=fy,
                fiscal_period="FY",
                revenue=_get("revenue"),
                net_income=_get("net_income"),
                operating_cash_flow=_get("operating_cash_flow"),
                capex=capex,
                dividends_paid=dividends_paid,
                dps_declared=_get("dps_declared"),
                dps_paid=_get("dps_paid"),
                total_debt=_get("total_debt"),
                cash=_get("cash"),
                equity=_get("equity"),
            )
            result.append(m)

        logger.info(
            "Extracted %d years of XBRL metrics for %s (%s)",
            len(result),
            ticker,
            entity_name,
        )
        return result

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def _get(self, url: str) -> Any:
        """Rate-limited GET with retry on transient errors."""
        self._enforce_rate_limit()
        logger.debug("GET %s", url)
        response = self._http.get(url)
        response.raise_for_status()
        return response.json()

    def _enforce_rate_limit(self) -> None:
        """Enforce minimum interval between EDGAR requests."""
        min_interval = 1.0 / self._rate_limit
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request_at = time.monotonic()

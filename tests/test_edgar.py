"""Tests for the EDGAR client — covers CIK resolution, metric extraction,
and edge cases (missing tags, negative capex normalization).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from incomos.data.edgar import EdgarClient, _XBRL_TAG_MAP
from incomos.core.types import XBRLMetrics


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_facts(
    revenue_values: list[tuple[int, float]] | None = None,
    ocf_values: list[tuple[int, float]] | None = None,
    capex_values: list[tuple[int, float]] | None = None,
    dividends_values: list[tuple[int, float]] | None = None,
    dps_values: list[tuple[int, float]] | None = None,
) -> dict:
    """Build a minimal EDGAR company facts structure."""

    def _entries(values: list[tuple[int, float]]) -> list[dict]:
        return [
            {"fy": fy, "val": val, "fp": "FY", "form": "10-K", "filed": f"{fy}-10-15"}
            for fy, val in values
        ]

    gaap: dict = {}
    if revenue_values:
        gaap["Revenues"] = {"units": {"USD": _entries(revenue_values)}}
    if ocf_values:
        gaap["NetCashProvidedByUsedInOperatingActivities"] = {
            "units": {"USD": _entries(ocf_values)}
        }
    if capex_values:
        gaap["PaymentsToAcquirePropertyPlantAndEquipment"] = {
            "units": {"USD": _entries(capex_values)}
        }
    if dividends_values:
        gaap["PaymentsOfDividendsCommonStock"] = {
            "units": {"USD": _entries(dividends_values)}
        }
    if dps_values:
        gaap["CommonStockDividendsPerShareDeclared"] = {
            "units": {"USD": _entries(dps_values)}
        }

    return {"entityName": "Test Corp", "facts": {"us-gaap": gaap}}


# ---------------------------------------------------------------------------
# CIK resolution
# ---------------------------------------------------------------------------

class TestResolveCik:
    def test_known_ticker(self, tmp_path, monkeypatch):
        """Known ticker resolves to correct CIK from cached map."""
        cache = tmp_path / "tickers.json"
        cache.write_text('{"AAPL": "0000320193"}')

        import incomos.data.edgar as edgar_mod
        monkeypatch.setattr(edgar_mod, "_TICKER_CACHE_PATH", cache)

        client = EdgarClient()
        client._ticker_map = {"AAPL": "0000320193"}
        assert client.resolve_cik("AAPL") == "0000320193"
        assert client.resolve_cik("aapl") == "0000320193"  # case-insensitive

    def test_unknown_ticker_raises(self):
        client = EdgarClient()
        client._ticker_map = {"AAPL": "0000320193"}
        # Force refresh is needed but mock it to return same map
        with patch.object(client, "_load_ticker_map", return_value={"AAPL": "0000320193"}):
            with pytest.raises(ValueError, match="not found in EDGAR"):
                client.resolve_cik("NOTREAL")


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------

class TestExtractAnnualSeries:
    def test_basic_extraction(self):
        facts = _make_facts(
            revenue_values=[(2021, 1e9), (2022, 2e9), (2023, 3e9)]
        )
        client = EdgarClient()
        series = client._extract_annual_series(facts, "revenue")
        assert series == [(2021, 1e9), (2022, 2e9), (2023, 3e9)]

    def test_deduplicates_amendments(self):
        """When a year has multiple filings (amendment), use the latest-filed."""
        gaap = {
            "Revenues": {
                "units": {
                    "USD": [
                        {"fy": 2023, "val": 1e9, "fp": "FY", "form": "10-K", "filed": "2024-01-10"},
                        {"fy": 2023, "val": 1.1e9, "fp": "FY", "form": "10-K/A", "filed": "2024-03-01"},
                    ]
                }
            }
        }
        facts = {"entityName": "Corp", "facts": {"us-gaap": gaap}}
        client = EdgarClient()
        series = client._extract_annual_series(facts, "revenue")
        assert len(series) == 1
        assert series[0] == (2023, 1.1e9)  # amendment wins

    def test_missing_tag_returns_empty(self):
        facts = {"entityName": "Corp", "facts": {"us-gaap": {}}}
        client = EdgarClient()
        series = client._extract_annual_series(facts, "revenue")
        assert series == []

    def test_skips_non_annual(self):
        """Quarterly entries (fp != FY) should be excluded."""
        gaap = {
            "Revenues": {
                "units": {
                    "USD": [
                        {"fy": 2023, "val": 1e9, "fp": "FY", "form": "10-K", "filed": "2024-01-10"},
                        {"fy": 2023, "val": 250e6, "fp": "Q1", "form": "10-Q", "filed": "2023-04-01"},
                    ]
                }
            }
        }
        facts = {"entityName": "Corp", "facts": {"us-gaap": gaap}}
        client = EdgarClient()
        series = client._extract_annual_series(facts, "revenue")
        assert len(series) == 1
        assert series[0][1] == 1e9


class TestGetAnnualMetrics:
    def test_computes_fcf(self):
        """FCF = operating_cash_flow - capex."""
        facts = _make_facts(
            ocf_values=[(2023, 10e9)],
            capex_values=[(2023, 2e9)],
        )
        client = EdgarClient()
        with patch.object(client, "get_company_facts", return_value=facts):
            result = client.get_annual_metrics("TEST", "0000000001", years=5)

        assert len(result) == 1
        m = result[0]
        assert m.free_cash_flow == pytest.approx(8e9)

    def test_normalizes_negative_capex(self):
        """CapEx reported as negative outflow should be stored as positive."""
        facts = _make_facts(
            ocf_values=[(2023, 10e9)],
            capex_values=[(2023, -2e9)],  # some companies report as negative
        )
        client = EdgarClient()
        with patch.object(client, "get_company_facts", return_value=facts):
            result = client.get_annual_metrics("TEST", "0000000001", years=5)

        assert result[0].capex == pytest.approx(2e9)
        assert result[0].free_cash_flow == pytest.approx(8e9)

    def test_fcf_payout_ratio(self):
        facts = _make_facts(
            ocf_values=[(2023, 10e9)],
            capex_values=[(2023, 2e9)],
            dividends_values=[(2023, 4e9)],
        )
        client = EdgarClient()
        with patch.object(client, "get_company_facts", return_value=facts):
            result = client.get_annual_metrics("TEST", "0000000001", years=5)

        assert result[0].fcf_payout_ratio == pytest.approx(0.5)  # 4B / 8B FCF

    def test_returns_empty_for_no_data(self):
        facts = {"entityName": "Corp", "facts": {"us-gaap": {}}}
        client = EdgarClient()
        with patch.object(client, "get_company_facts", return_value=facts):
            result = client.get_annual_metrics("TEST", "0000000001", years=5)

        assert result == []

    def test_limits_to_requested_years(self):
        facts = _make_facts(
            revenue_values=[(2019, 1e9), (2020, 2e9), (2021, 3e9), (2022, 4e9), (2023, 5e9), (2024, 6e9)]
        )
        client = EdgarClient()
        with patch.object(client, "get_company_facts", return_value=facts):
            result = client.get_annual_metrics("TEST", "0000000001", years=3)

        assert len(result) == 3
        assert [m.fiscal_year for m in result] == [2022, 2023, 2024]

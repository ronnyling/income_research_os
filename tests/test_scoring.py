"""Tests for income and business quality scoring.

These are unit tests using synthetic XBRL metrics — no network calls.
"""

from __future__ import annotations

import pytest
from incomos.core.types import XBRLMetrics
from incomos.scoring.income import compute_income_quality
from incomos.scoring.business import compute_business_quality


def _make_metrics(
    fiscal_years: list[int],
    revenues: list[float | None],
    op_cash_flows: list[float],
    capexes: list[float],
    dividends: list[float | None],
    dps: list[float | None] | None = None,
) -> list[XBRLMetrics]:
    result = []
    for i, yr in enumerate(fiscal_years):
        result.append(XBRLMetrics(
            ticker="TEST",
            cik="0000000001",
            fiscal_year=yr,
            fiscal_period="FY",
            revenue=revenues[i] if revenues else None,
            operating_cash_flow=op_cash_flows[i],
            capex=capexes[i],
            dividends_paid=dividends[i] if dividends else None,
            dps_declared=(dps[i] if dps else None),
        ))
    return result


# ---- Income Quality Tests -----------------------------------------------

class TestIncomeQuality:

    def test_high_quality_income_stock(self):
        """Strong dividend history, low payout ratio, consistent FCF."""
        metrics = _make_metrics(
            fiscal_years=[2020, 2021, 2022, 2023, 2024],
            revenues=[10e9] * 5,
            op_cash_flows=[3e9, 3.2e9, 3.4e9, 3.6e9, 3.8e9],
            capexes=[0.5e9] * 5,
            dividends=[1e9, 1.05e9, 1.1e9, 1.15e9, 1.2e9],
        )
        score, breakdown = compute_income_quality(metrics)
        assert score >= 75, f"Expected high income score, got {score}"
        assert breakdown["dividend_continuity"]["years"] == 5
        assert breakdown["fcf_consistency"]["positive_years"] == 5

    def test_dividend_cut_penalised(self):
        """Dividend cut in final year should reduce continuity score."""
        metrics = _make_metrics(
            fiscal_years=[2020, 2021, 2022, 2023, 2024],
            revenues=[10e9] * 5,
            op_cash_flows=[3e9] * 5,
            capexes=[0.5e9] * 5,
            dividends=[1e9, 1e9, 1e9, 0.3e9, 0.0],  # suspended
        )
        score, breakdown = compute_income_quality(metrics)
        assert score < 70, f"Should be penalised for dividend cut: {score}"

    def test_no_dividends_zero_score_continuity(self):
        """No dividends at all → zero dividend continuity points."""
        metrics = _make_metrics(
            fiscal_years=[2020, 2021, 2022, 2023, 2024],
            revenues=[10e9] * 5,
            op_cash_flows=[3e9] * 5,
            capexes=[0.5e9] * 5,
            dividends=[None, None, None, None, None],
        )
        score, breakdown = compute_income_quality(metrics)
        assert breakdown["dividend_continuity"]["years"] == 0
        assert breakdown["dividend_continuity"]["pts"] == 0

    def test_high_payout_ratio_penalised(self):
        """Payout ratio ≥ 90% should get 0 points on that component."""
        metrics = _make_metrics(
            fiscal_years=[2020, 2021, 2022, 2023, 2024],
            revenues=[10e9] * 5,
            op_cash_flows=[2e9] * 5,
            capexes=[0.3e9] * 5,  # FCF = 1.7e9
            dividends=[1.6e9] * 5,  # payout = 94%
        )
        score, breakdown = compute_income_quality(metrics)
        assert breakdown["fcf_payout_ratio"]["pts"] == 0

    def test_low_payout_ratio_max_pts(self):
        """Payout ratio < 30% should get max 25 points."""
        metrics = _make_metrics(
            fiscal_years=[2020, 2021, 2022, 2023, 2024],
            revenues=[10e9] * 5,
            op_cash_flows=[5e9] * 5,
            capexes=[0.5e9] * 5,  # FCF = 4.5e9
            dividends=[1e9] * 5,  # payout = 22%
        )
        score, breakdown = compute_income_quality(metrics)
        assert breakdown["fcf_payout_ratio"]["pts"] == 25

    def test_empty_metrics_returns_zero(self):
        score, breakdown = compute_income_quality([])
        assert score == 0.0

    def test_dps_proxy_dividend_continuity(self):
        """When dividends_paid is None but dps_declared > 0, should count as dividend year."""
        metrics = _make_metrics(
            fiscal_years=[2020, 2021, 2022, 2023, 2024],
            revenues=[10e9] * 5,
            op_cash_flows=[3e9] * 5,
            capexes=[0.5e9] * 5,
            dividends=[None, None, None, None, None],  # no aggregate data
            dps=[1.5, 1.55, 1.60, 1.65, 1.70],
        )
        score, breakdown = compute_income_quality(metrics)
        assert breakdown["dividend_continuity"]["years"] == 5


# ---- Business Quality Tests ---------------------------------------------

class TestBusinessQuality:

    def test_high_quality_business(self):
        """Strong revenue growth, high FCF margin, improving balance sheet."""
        metrics = _make_metrics(
            fiscal_years=[2020, 2021, 2022, 2023, 2024],
            revenues=[10e9, 11.2e9, 12.5e9, 14e9, 15.7e9],
            op_cash_flows=[3e9, 3.4e9, 3.8e9, 4.2e9, 4.7e9],
            capexes=[0.5e9] * 5,
            dividends=[1e9] * 5,
        )
        # Artificially set debt via total_debt
        for m in metrics:
            m.total_debt = 5e9
            m.cash = 2e9
        score, breakdown = compute_business_quality(metrics)
        assert score >= 65, f"Expected high business score, got {score}"

    def test_negative_revenue_growth_penalised(self):
        """Declining revenue should reduce score."""
        metrics = _make_metrics(
            fiscal_years=[2020, 2021, 2022, 2023, 2024],
            revenues=[15e9, 13e9, 12e9, 11e9, 10e9],
            op_cash_flows=[3e9] * 5,
            capexes=[0.5e9] * 5,
            dividends=[1e9] * 5,
        )
        score, breakdown = compute_business_quality(metrics)
        assert breakdown["revenue_cagr"]["cagr"] < 0
        assert breakdown["revenue_cagr"]["pts"] <= 8

    def test_negative_fcf_penalised(self):
        """Negative FCF margin should get 0 margin points."""
        metrics = _make_metrics(
            fiscal_years=[2020, 2021, 2022, 2023, 2024],
            revenues=[10e9] * 5,
            op_cash_flows=[0.3e9] * 5,
            capexes=[0.8e9] * 5,  # capex > op_cashflow → negative FCF
            dividends=[1e9] * 5,
        )
        score, breakdown = compute_business_quality(metrics)
        assert breakdown["fcf_margin"]["pts"] == 0

    def test_missing_revenue_no_partial_credit(self):
        """Missing revenue data → 0 pts (no partial credit in production)."""
        metrics = _make_metrics(
            fiscal_years=[2020, 2021, 2022, 2023, 2024],
            revenues=[None] * 5,
            op_cash_flows=[3e9] * 5,
            capexes=[0.5e9] * 5,
            dividends=[1e9] * 5,
        )
        score, breakdown = compute_business_quality(metrics)
        assert breakdown["revenue_cagr"].get("note") == "data gap"
        assert breakdown["revenue_cagr"]["pts"] == 0  # no partial credit

    def test_empty_metrics_returns_zero(self):
        score, breakdown = compute_business_quality([])
        assert score == 0.0

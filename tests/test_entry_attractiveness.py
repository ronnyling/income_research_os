"""Tests for the Entry Attractiveness scorer (Stage 1→2 replacement)."""

from __future__ import annotations

import pytest
from unittest.mock import patch

from incomos.funnel.entry_attractiveness import (
    _score_drawdown,
    _score_yield_expansion,
    _score_dividend_growth,
    _score_valuation,
    _score_trend,
    compute_entry_attractiveness,
    EntryAttractivenessResult,
)
from incomos.data.market import PriceSnapshot
from incomos.core.types import XBRLMetrics


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_snap(**kwargs) -> PriceSnapshot:
    defaults = dict(
        ticker="TEST", current_price=100.0, week52_high=120.0, week52_low=80.0,
        pct_below_52w_high=0.10, rsi_14=45.0, volume_ratio=1.0,
        price_above_50sma=False, price_above_200sma=True,
        price_trend="RANGING", data_quality="GOOD",
        current_yield=0.03, avg_yield_5yr=0.025, yield_expansion_ratio=1.20,
    )
    defaults.update(kwargs)
    return PriceSnapshot(**defaults)


def _make_metrics(fiscal_year=2025, dps_declared=2.0, revenue=100.0, net_income=20.0,
                  fcf=15.0, **kwargs) -> XBRLMetrics:
    defaults = dict(
        ticker="TEST", cik="0000000000", fiscal_year=fiscal_year, fiscal_period="FY",
        revenue=revenue, net_income=net_income, operating_cash_flow=fcf, capex=0.0,
        dividends_paid=10.0, dps_declared=dps_declared, dps_paid=dps_declared,
    )
    defaults.update(kwargs)
    return XBRLMetrics(**defaults)


# ---------------------------------------------------------------------------
# _score_drawdown
# ---------------------------------------------------------------------------

class TestScoreDrawdown:
    def test_no_drawdown(self):
        score, tag = _score_drawdown(0.0)
        assert score == 0.0
        assert tag is None

    def test_small_drawdown(self):
        score, tag = _score_drawdown(0.03)
        assert 0 < score < 20
        assert tag is None

    def test_moderate_drawdown(self):
        score, tag = _score_drawdown(0.10)
        assert 30 <= score <= 40
        assert tag == "DIP"

    def test_deep_drawdown(self):
        score, tag = _score_drawdown(0.20)
        assert 60 <= score <= 70
        assert tag == "DEEP_DIP"

    def test_extreme_drawdown(self):
        score, tag = _score_drawdown(0.45)
        assert score == 100.0
        assert tag == "DEEP_DIP"

    def test_monotonicity(self):
        """Higher drawdown should always score higher."""
        scores = [_score_drawdown(d)[0] for d in [0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40]]
        for i in range(len(scores) - 1):
            assert scores[i] <= scores[i + 1], f"Drawdown not monotonic at index {i}"


# ---------------------------------------------------------------------------
# _score_yield_expansion
# ---------------------------------------------------------------------------

class TestScoreYieldExpansion:
    def test_no_data(self):
        score, tag = _score_yield_expansion(None, None, None, 1.10)
        assert score == 0.0
        assert tag is None

    def test_below_min_ratio(self):
        score, tag = _score_yield_expansion(0.02, 0.025, 0.80, 1.10)
        assert score == 0.0
        assert tag is None

    def test_at_expansion_threshold(self):
        score, tag = _score_yield_expansion(0.03, 0.027, 1.10, 1.10)
        assert score >= 30
        assert tag == "YIELD_EXPANSION"

    def test_strong_expansion(self):
        score, tag = _score_yield_expansion(0.04, 0.025, 1.60, 1.10)
        assert score == 100.0
        assert tag == "YIELD_EXPANSION"

    def test_zero_yield(self):
        score, tag = _score_yield_expansion(0.0, 0.025, 0.0, 1.10)
        assert score == 0.0
        assert tag is None


# ---------------------------------------------------------------------------
# _score_dividend_growth
# ---------------------------------------------------------------------------

class TestScoreDividendGrowth:
    def test_no_metrics(self):
        score, tag = _score_dividend_growth([], 0.0)
        assert score == 0.0
        assert tag is None

    def test_single_metric(self):
        metrics = [_make_metrics(fiscal_year=2025, dps_declared=2.0)]
        score, tag = _score_dividend_growth(metrics, 0.0)
        assert score == 0.0
        assert tag is None

    def test_growing_dividend(self):
        metrics = [
            _make_metrics(fiscal_year=2022, dps_declared=1.50),
            _make_metrics(fiscal_year=2023, dps_declared=1.70),
            _make_metrics(fiscal_year=2024, dps_declared=1.90),
            _make_metrics(fiscal_year=2025, dps_declared=2.10),
        ]
        score, tag = _score_dividend_growth(metrics, 0.0)
        assert score > 40
        assert tag == "DIVIDEND_GROWTH"

    def test_flat_dividend(self):
        metrics = [
            _make_metrics(fiscal_year=2022, dps_declared=2.0),
            _make_metrics(fiscal_year=2023, dps_declared=2.0),
            _make_metrics(fiscal_year=2024, dps_declared=2.0),
            _make_metrics(fiscal_year=2025, dps_declared=2.0),
        ]
        score, tag = _score_dividend_growth(metrics, 0.0)
        # CAGR = 0%, min_cagr = 0.0, so it gets a low score
        assert score >= 0
        assert tag is None  # cagr not > 0

    def test_declining_dividend(self):
        metrics = [
            _make_metrics(fiscal_year=2022, dps_declared=2.50),
            _make_metrics(fiscal_year=2025, dps_declared=1.50),
        ]
        score, tag = _score_dividend_growth(metrics, 0.0)
        assert score == 0.0
        assert tag is None


# ---------------------------------------------------------------------------
# _score_valuation
# ---------------------------------------------------------------------------

class TestScoreValuation:
    def test_no_metrics(self):
        score, tag = _score_valuation([], 100.0, 0.10)
        assert score == 0.0
        assert tag is None

    def test_negative_net_income(self):
        metrics = [_make_metrics(net_income=-10.0)]
        score, tag = _score_valuation(metrics, 100.0, 0.10)
        assert score == 0.0
        assert tag is None

    def test_high_fcf_margin_with_price_decline(self):
        metrics = [
            _make_metrics(fiscal_year=2022, revenue=100.0, net_income=20.0, fcf=25.0),
            _make_metrics(fiscal_year=2025, revenue=120.0, net_income=25.0, fcf=30.0),
        ]
        score, tag = _score_valuation(metrics, 90.0, 0.15)
        assert score >= 50
        assert tag == "UNDERVALUED"

    def test_low_fcf_margin(self):
        metrics = [
            _make_metrics(fiscal_year=2022, revenue=100.0, net_income=2.0, fcf=1.0),
            _make_metrics(fiscal_year=2025, revenue=110.0, net_income=3.0, fcf=2.0),
        ]
        score, tag = _score_valuation(metrics, 100.0, 0.05)
        assert score < 50
        assert tag is None


# ---------------------------------------------------------------------------
# _score_trend
# ---------------------------------------------------------------------------

class TestScoreTrend:
    def test_trending_down(self):
        score, tag = _score_trend("TRENDING_DOWN")
        assert score == 100.0
        assert tag == "TRENDING_DOWN"

    def test_ranging(self):
        score, tag = _score_trend("RANGING")
        assert score == 30.0
        assert tag is None

    def test_trending_up(self):
        score, tag = _score_trend("TRENDING_UP")
        assert score == 0.0
        assert tag is None


# ---------------------------------------------------------------------------
# compute_entry_attractiveness (integration)
# ---------------------------------------------------------------------------

class TestComputeEntryAttractiveness:
    def test_basic_scoring(self):
        snap = _make_snap(
            pct_below_52w_high=0.20,
            price_trend="TRENDING_DOWN",
            current_yield=0.035,
            avg_yield_5yr=0.025,
            yield_expansion_ratio=1.40,
        )
        metrics = [
            _make_metrics(fiscal_year=2022, dps_declared=1.50, revenue=90.0, net_income=18.0, fcf=14.0),
            _make_metrics(fiscal_year=2025, dps_declared=2.10, revenue=120.0, net_income=25.0, fcf=30.0),
        ]
        result = compute_entry_attractiveness("TEST", snap, metrics)
        assert isinstance(result, EntryAttractivenessResult)
        assert 0 <= result.score <= 100
        assert result.score > 50  # Should be attractive
        assert "DEEP_DIP" in result.tags
        assert "YIELD_EXPANSION" in result.tags
        assert "TRENDING_DOWN" in result.tags
        assert result.meets_floor is True

    def test_no_drawdown_no_yield(self):
        """Stock near highs with average yield should score low."""
        snap = _make_snap(
            pct_below_52w_high=0.01,
            price_trend="TRENDING_UP",
            current_yield=0.02,
            avg_yield_5yr=0.02,
            yield_expansion_ratio=1.0,
        )
        metrics = [_make_metrics()]
        result = compute_entry_attractiveness("TEST", snap, metrics)
        assert result.score < 30

    def test_failed_quality_raises(self):
        snap = _make_snap(data_quality="FAILED")
        with pytest.raises(Exception):  # PriceDataUnavailableError
            compute_entry_attractiveness("TEST", snap, [])

    def test_tags_populated(self):
        snap = _make_snap(
            pct_below_52w_high=0.12,
            price_trend="TRENDING_DOWN",
            current_yield=0.04,
            avg_yield_5yr=0.025,
            yield_expansion_ratio=1.60,
        )
        metrics = [
            _make_metrics(fiscal_year=2022, dps_declared=1.50, revenue=90.0, net_income=18.0, fcf=20.0),
            _make_metrics(fiscal_year=2025, dps_declared=2.20, revenue=130.0, net_income=30.0, fcf=35.0),
        ]
        result = compute_entry_attractiveness("TEST", snap, metrics)
        assert len(result.tags) >= 3  # DEEP_DIP, YIELD_EXPANSION, TRENDING_DOWN at minimum

    def test_component_scores_sum_reasonably(self):
        snap = _make_snap()
        metrics = [_make_metrics()]
        result = compute_entry_attractiveness("TEST", snap, metrics)
        # All components should be 0-100
        for key, val in result.component_scores.items():
            assert 0 <= val <= 100, f"{key} = {val} out of range"

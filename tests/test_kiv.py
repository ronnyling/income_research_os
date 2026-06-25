"""Tests for dip trigger and KIV lifecycle logic.

Unit tests only — no network calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from incomos.data.market import PriceSnapshot
from incomos.core.exceptions import PriceDataUnavailableError
from incomos.funnel.dip_trigger import check_dip_trigger
from incomos.funnel.kiv import (
    evaluate_kiv_entry,
    evaluate_kiv_demotion,
    evaluate_candidate_demotion,
    evaluate_finalist_demotion,
)
from incomos.core.types import FunnelStage, XBRLMetrics


def _snap(ticker="TEST", current=90.0, high52=100.0, rsi=45.0, vol_ratio=1.5,
          trend="RANGING", quality="GOOD") -> PriceSnapshot:
    pct_below = (high52 - current) / high52 if high52 > 0 else 0
    return PriceSnapshot(
        ticker=ticker, current_price=current, week52_high=high52,
        week52_low=current * 0.8, pct_below_52w_high=pct_below,
        rsi_14=rsi, volume_ratio=vol_ratio, price_above_50sma=False,
        price_above_200sma=False, price_trend=trend, data_quality=quality,
    )


def _metrics_list(fcf_positive=True, dividends_paid=1e9) -> list[XBRLMetrics]:
    yr = 2024
    m = XBRLMetrics(
        ticker="TEST", cik="0000000001", fiscal_year=yr, fiscal_period="FY",
        operating_cash_flow=3e9 if fcf_positive else 0.3e9,
        capex=0.5e9 if fcf_positive else 2e9,
        dividends_paid=dividends_paid,
    )
    return [m]


# ---- Dip trigger tests --------------------------------------------------

class TestDipTrigger:

    def test_no_trigger_shallow_dip(self):
        snap = _snap(current=97.0, high52=100.0, rsi=55.0, vol_ratio=0.9)
        result = check_dip_trigger(snap)
        assert not result.triggered
        assert result.trigger_strength == "NONE"

    def test_soft_trigger_fires_with_moderate_rsi(self):
        """8% dip + RSI < 50 → MODERATE trigger."""
        snap = _snap(current=92.0, high52=100.0, rsi=47.0, vol_ratio=1.0)
        result = check_dip_trigger(snap)
        assert result.triggered
        assert result.trigger_strength == "MODERATE"

    def test_strong_trigger(self):
        """20% dip + RSI < 40 + elevated volume → STRONG."""
        snap = _snap(current=78.0, high52=100.0, rsi=35.0, vol_ratio=2.2)
        result = check_dip_trigger(snap)
        assert result.triggered
        assert result.trigger_strength == "STRONG"

    def test_failed_data_quality_raises(self):
        snap = _snap(quality="FAILED")
        snap.data_quality = "FAILED"
        with pytest.raises(PriceDataUnavailableError):
            check_dip_trigger(snap)

    def test_signals_list_populated(self):
        snap = _snap(current=80.0, high52=100.0, rsi=32.0, vol_ratio=1.5)
        result = check_dip_trigger(snap)
        assert len(result.signals) >= 2


# ---- KIV lifecycle tests ------------------------------------------------

class TestKIVLifecycle:

    def test_promotion_when_triggered(self):
        snap = _snap(current=78.0, high52=100.0, rsi=35.0, vol_ratio=2.0)
        dip = check_dip_trigger(snap)
        assert dip.triggered
        transition = evaluate_kiv_entry("TEST", dip, FunnelStage.PROSPECTS)
        assert transition is not None
        assert transition.to_stage == FunnelStage.KIV
        assert transition.direction == "PROMOTE"

    def test_no_promotion_when_not_triggered(self):
        snap = _snap(current=98.0, high52=100.0, rsi=60.0)
        dip = check_dip_trigger(snap)
        assert not dip.triggered
        transition = evaluate_kiv_entry("TEST", dip, FunnelStage.PROSPECTS)
        assert transition is None

    def test_no_promotion_when_not_in_prospects(self):
        """Only PROSPECTS can be promoted to KIV via dip trigger."""
        snap = _snap(current=78.0, high52=100.0, rsi=32.0, vol_ratio=2.0)
        dip = check_dip_trigger(snap)
        transition = evaluate_kiv_entry("TEST", dip, FunnelStage.KIV)
        assert transition is None

    def test_demotion_negative_fcf(self):
        """FCF turned negative should trigger demotion to REJECTED."""
        metrics = [XBRLMetrics(
            ticker="TEST", cik="0000000001", fiscal_year=2024, fiscal_period="FY",
            operating_cash_flow=0.3e9,
            capex=2e9,  # capex > op_cashflow → negative FCF
            dividends_paid=0.5e9,
        )]
        transition = evaluate_kiv_demotion(
            "TEST",
            entered_kiv_at=datetime.now(timezone.utc),
            latest_metrics=metrics,
        )
        assert transition is not None
        assert transition.to_stage == FunnelStage.REJECTED
        assert "FCF negative" in transition.reason

    def test_demotion_dividend_suspended(self):
        """Dividend dropping from >0 to 0 should trigger demotion."""
        metrics = [
            XBRLMetrics(ticker="TEST", cik="0000000001", fiscal_year=2023, fiscal_period="FY",
                        operating_cash_flow=3e9, capex=0.5e9, dividends_paid=1e9),
            XBRLMetrics(ticker="TEST", cik="0000000001", fiscal_year=2024, fiscal_period="FY",
                        operating_cash_flow=3e9, capex=0.5e9, dividends_paid=0),
        ]
        transition = evaluate_kiv_demotion("TEST", datetime.now(timezone.utc), metrics)
        assert transition is not None
        assert "suspended" in transition.reason.lower()

    def test_no_demotion_healthy_stock(self):
        """Healthy FCF and dividends → no demotion."""
        metrics = _metrics_list(fcf_positive=True, dividends_paid=1e9)
        transition = evaluate_kiv_demotion("TEST", datetime.now(timezone.utc), metrics)
        assert transition is None  # healthy → no demotion

    def test_8k_fraud_flag_triggers_demotion(self):
        """8-K fraud flag must trigger demotion."""
        metrics = _metrics_list(fcf_positive=True, dividends_paid=1e9)
        transition = evaluate_kiv_demotion(
            "TEST", datetime.now(timezone.utc), metrics,
            recent_8k_flags=["Restatement of financial results due to accounting fraud"],
        )
        assert transition is not None
        assert transition.to_stage == FunnelStage.REJECTED

    def test_finalist_demotion_price_above_target(self):
        """Finalist demoted back to KIV when price hasn't dipped to target."""
        t = evaluate_finalist_demotion("TEST", current_price=105.0, target_entry_price=90.0)
        assert t is not None
        assert t.to_stage == FunnelStage.KIV

    def test_finalist_demotion_price_at_target(self):
        """No demotion when price is at or below target."""
        t = evaluate_finalist_demotion("TEST", current_price=89.0, target_entry_price=90.0)
        assert t is None

    def test_candidate_demotion_macro_unfavorable(self):
        """Candidate demoted when RECESSION_RISK at high confidence."""
        from incomos.core.types import (
            MacroAxisReading, MacroRegimeContract, MacroUsagePolicy,
            SectorImpact, GrowthState, RatesState,
        )
        growth = MacroAxisReading(state=GrowthState.RECESSION_RISK, confidence=0.82, severity=0.7)
        rates = MacroAxisReading(state=RatesState.RATE_SHOCK_UP, confidence=0.75, severity=0.6)
        macro = MacroRegimeContract(
            as_of=datetime.now(timezone.utc),
            primary_regime="RECESSION_RISK",
            primary_confidence=0.82,
            market_structure=MacroAxisReading(state="TRENDING_DOWN", confidence=0.7, severity=0.5),
            growth=growth,
            rates=rates,
            financial_conditions=MacroAxisReading(state="TIGHTENING", confidence=0.7, severity=0.5),
            sector_impact=SectorImpact(sector="General", macro_alignment=0.5,
                                       peer_drawdown_breadth=0.5, sector_override_eligible=False),
            usage_policy=MacroUsagePolicy(),
            evidence=[],
        )
        t = evaluate_candidate_demotion("TEST", macro, fundamentals_intact=True)
        assert t is not None
        assert t.to_stage == FunnelStage.KIV

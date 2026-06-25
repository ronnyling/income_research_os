"""Tests for the Stage 0→1 quality screen.

Covers: PASS path, FAIL paths (no dividends, negative FCF, high payout ratio),
XBRL data gap handling, DPS-as-fallback, and edge cases.
"""

from __future__ import annotations

from datetime import datetime, timezone

from incomos.core.types import XBRLMetrics
from incomos.screening.stage01 import run_quality_screen


def _m(
    fy: int,
    ocf: float | None = 10e9,
    capex: float | None = 1e9,
    dividends_paid: float | None = 2e9,
    dps_declared: float | None = 1.50,
    dps_paid: float | None = None,
    revenue: float | None = 50e9,
    net_income: float | None = 5e9,
) -> XBRLMetrics:
    """Build a test XBRLMetrics fixture. All values represent a healthy company by default."""
    return XBRLMetrics(
        ticker="TEST",
        cik="0000000001",
        fiscal_year=fy,
        fiscal_period="FY",
        revenue=revenue,
        net_income=net_income,
        operating_cash_flow=ocf,
        capex=capex,
        dividends_paid=dividends_paid,
        dps_declared=dps_declared,
        dps_paid=dps_paid,
    )


class TestQualityScreenPass:
    def test_healthy_dividend_compounder_passes(self):
        metrics = [_m(fy) for fy in range(2019, 2024)]
        result = run_quality_screen("TEST", metrics)
        assert result.passed
        assert all(result.checks.values())

    def test_passes_with_exactly_min_dividend_years(self):
        """3 dividend years, 2 without = should pass (min is 3)."""
        metrics = [
            _m(2019, dividends_paid=0, dps_declared=None),
            _m(2020, dividends_paid=0, dps_declared=None),
            _m(2021),
            _m(2022),
            _m(2023),
        ]
        result = run_quality_screen("TEST", metrics)
        assert result.checks["dividend_continuity"] is True


class TestQualityScreenFail:
    def test_no_dividends_fails(self):
        metrics = [_m(fy, dividends_paid=0, dps_declared=None, dps_paid=None) for fy in range(2019, 2024)]
        result = run_quality_screen("TEST", metrics)
        assert not result.passed
        assert result.checks["dividend_continuity"] is False

    def test_negative_fcf_fails(self):
        # OCF < CapEx → FCF negative
        metrics = [_m(fy, ocf=1e9, capex=5e9) for fy in range(2019, 2024)]
        result = run_quality_screen("TEST", metrics)
        assert result.checks["fcf_positive"] is False

    def test_high_payout_ratio_fails(self):
        # FCF = 1B, dividends_paid = 1.5B → payout ratio = 150% > 90% ceiling
        metrics = [_m(fy, ocf=2e9, capex=1e9, dividends_paid=1.5e9) for fy in range(2019, 2024)]
        result = run_quality_screen("TEST", metrics)
        assert result.checks["fcf_payout_ratio"] is False

    def test_zero_revenue_fails(self):
        metrics = [_m(fy, revenue=0) for fy in range(2019, 2024)]
        result = run_quality_screen("TEST", metrics)
        assert result.checks["revenue_positive"] is False

    def test_no_data_fails_gracefully(self):
        result = run_quality_screen("EMPTY", [])
        assert not result.passed
        assert "No XBRL data found" in result.notes[0]


class TestXBRLGapHandling:
    def test_dps_as_dividend_proxy(self):
        """If dividends_paid is None but dps_declared > 0, counts as dividend year."""
        metrics = [
            _m(fy, dividends_paid=None, dps_declared=1.50)  # No aggregate, but DPS available
            for fy in range(2019, 2024)
        ]
        result = run_quality_screen("TEST", metrics)
        assert result.checks["dividend_continuity"] is True

    def test_all_none_flags_gap(self):
        """Years where ALL dividend fields are None generate a data gap note."""
        metrics = [
            _m(2021),
            _m(2022),
            _m(2023, dividends_paid=None, dps_declared=None, dps_paid=None),
            _m(2024, dividends_paid=None, dps_declared=None, dps_paid=None),
            _m(2025, dividends_paid=None, dps_declared=None, dps_paid=None),
        ]
        result = run_quality_screen("TEST", metrics)
        # Only 2 confirmed dividend years — fails, but note mentions XBRL tag gap
        assert result.checks["dividend_continuity"] is False
        assert any("XBRL tag gap" in note for note in result.notes)

    def test_payout_ratio_skipped_when_fcf_missing(self):
        """If FCF data is incomplete, payout_ratio check fails with a clear message."""
        metrics = [
            _m(fy, ocf=None, capex=None, dividends_paid=2e9)
            for fy in range(2019, 2024)
        ]
        result = run_quality_screen("TEST", metrics)
        assert result.checks["fcf_payout_ratio"] is False
        assert any("Cannot compute" in note for note in result.notes)

    def test_negative_fcf_payout_ratio_is_none(self):
        """FCF negative → payout ratio is None (not computable, not safe)."""
        m = _m(2023, ocf=1e9, capex=3e9, dividends_paid=1e9)
        assert m.free_cash_flow == -2e9  # type: ignore
        assert m.fcf_payout_ratio is None


class TestRefreshLogic:
    """Tests for the staleness-triggered refresh engine."""

    def test_stale_when_never_refreshed(self):
        from incomos.funnel.refresh import is_stale
        from incomos.core.types import RefreshRecord

        record = RefreshRecord(data_type="macro_regime", last_refresh=None)
        assert is_stale(record, max_age_hours=24)

    def test_fresh_record_not_stale(self):
        from incomos.funnel.refresh import is_stale
        from incomos.core.types import RefreshRecord

        record = RefreshRecord(
            data_type="macro_regime",
            last_refresh=datetime.now(timezone.utc),
        )
        assert not is_stale(record, max_age_hours=24)

    def test_build_refresh_queue_prioritized(self):
        from incomos.funnel.refresh import build_refresh_queue
        from incomos.core.types import RefreshRecord

        records = [
            RefreshRecord(data_type="universe_quality", last_refresh=None),
            RefreshRecord(data_type="macro_regime", last_refresh=None),
            RefreshRecord(data_type="kiv_price", last_refresh=None),
        ]
        queue = build_refresh_queue(records)
        assert [t.data_type for t in queue] == ["macro_regime", "kiv_price", "universe_quality"]

    def test_fresh_records_not_queued(self):
        from incomos.funnel.refresh import build_refresh_queue
        from incomos.core.types import RefreshRecord

        fresh = RefreshRecord(
            data_type="macro_regime",
            last_refresh=datetime.now(timezone.utc),
        )
        queue = build_refresh_queue([fresh])
        assert queue == []

    def test_kiv_ttl_detects_expired(self):
        from incomos.funnel.refresh import check_kiv_ttl
        from datetime import timedelta

        old_entry = datetime.now(timezone.utc) - timedelta(days=95)
        recent_entry = datetime.now(timezone.utc) - timedelta(days=10)

        demoted = check_kiv_ttl([("OLD_STOCK", old_entry), ("NEW_STOCK", recent_entry)])
        assert "OLD_STOCK" in demoted
        assert "NEW_STOCK" not in demoted

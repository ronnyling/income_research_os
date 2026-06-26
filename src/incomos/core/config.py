"""Application settings — loaded from environment / .env file.

All calibration thresholds live here as named fields grouped by concern.
No module may hardcode a threshold that influences scoring, sizing, or
funnel logic.  Override any value via .env or env var using the nested
delimiter convention (env_nested_delimiter = "__"):

  DIP_TRIGGER__THRESHOLD_SOFT=0.10
  SCORING_WEIGHTS__INCOME=0.35
  MACRO__RECESSION_PROB_THRESHOLD=0.30
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ═══════════════════════════════════════════════════════════════════════════
# Threshold sub-models — one per concern
# ═══════════════════════════════════════════════════════════════════════════

class DipTriggerCfg(BaseModel):
    """Stage 1→2 dip trigger thresholds (legacy — prefer EntryAttractivenessCfg)."""
    threshold_soft: float = Field(default=0.08, ge=0.01, le=0.50)
    threshold_hard: float = Field(default=0.15, ge=0.01, le=0.75)
    rsi_soft: float = Field(default=50.0, ge=1.0, le=100.0)
    rsi_hard: float = Field(default=40.0, ge=1.0, le=99.0)
    volume_elevated: float = Field(default=1.3, ge=0.5, le=10.0)


class EntryAttractivenessCfg(BaseModel):
    """Stage 1→2 entry attractiveness scoring weights and thresholds.

    Replaces the binary dip trigger with a continuous 0-100 score.
    Stocks are ranked by this score and the top N are promoted to KIV
    (where N = kiv_target_size in Settings).
    """
    # Component weights (must sum to 1.0)
    weight_drawdown: float = Field(default=0.30, ge=0.0, le=1.0,
        description="Weight for price drawdown from 52W high")
    weight_yield_expansion: float = Field(default=0.25, ge=0.0, le=1.0,
        description="Weight for current yield vs historical average")
    weight_dividend_growth: float = Field(default=0.20, ge=0.0, le=1.0,
        description="Weight for DPS CAGR trajectory")
    weight_valuation: float = Field(default=0.15, ge=0.0, le=1.0,
        description="Weight for earnings yield / value signal")
    weight_trend: float = Field(default=0.10, ge=0.0, le=1.0,
        description="Weight for price trend (TRENDING_DOWN bonus)")

    # Minimum floor — don't promote stocks below this even to fill target count
    min_attractiveness_score: float = Field(default=20.0, ge=0.0, le=100.0)

    # Yield expansion: how much above historical average counts as attractive
    yield_expansion_min_ratio: float = Field(default=1.10, ge=1.0, le=3.0,
        description="Current yield / 5yr avg yield must be >= this to score (1.10 = 10% above avg)")

    # Dividend growth floor — DPS must have grown at least this much over 3yr
    div_growth_min_cagr: float = Field(default=0.0, le=0.50,
        description="Minimum DPS CAGR to score on dividend growth (0.0 = any positive growth)")

    # Drawdown scoring breakpoints (configurable shape)
    drawdown_t1: float = Field(default=0.02, ge=0.0, le=0.50,
        description="Drawdown threshold for minimal score")
    drawdown_t2: float = Field(default=0.05, ge=0.0, le=0.50)
    drawdown_t3: float = Field(default=0.10, ge=0.0, le=0.50)
    drawdown_t4: float = Field(default=0.15, ge=0.0, le=0.50)
    drawdown_t5: float = Field(default=0.20, ge=0.0, le=0.50)
    drawdown_t6: float = Field(default=0.30, ge=0.0, le=0.50)
    drawdown_t7: float = Field(default=0.40, ge=0.0, le=0.50)

    @model_validator(mode="after")
    def _check_sum(self) -> "EntryAttractivenessCfg":
        total = (
            self.weight_drawdown + self.weight_yield_expansion
            + self.weight_dividend_growth + self.weight_valuation
            + self.weight_trend
        )
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"EntryAttractiveness weights must sum to 1.0, got {total:.3f}"
            )
        return self


class KivCfg(BaseModel):
    """KIV basket management settings."""
    target_size: int = Field(default=20, ge=5, le=500,
        description="Target number of stocks in the KIV basket")
    hysteresis_days: int = Field(default=14, ge=1, le=90,
        description="Days a stock stays in KIV before eligible for demotion by score drop")


class ScoringWeightsCfg(BaseModel):
    """Opportunity Score formula weights (locked per architecture; sum must = 1.0)."""
    income: float = Field(default=0.30, ge=0.0, le=1.0)
    business: float = Field(default=0.30, ge=0.0, le=1.0)
    dip: float = Field(default=0.30, ge=0.0, le=1.0)
    oversold: float = Field(default=0.10, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_sum(self) -> "ScoringWeightsCfg":
        total = self.income + self.business + self.dip + self.oversold
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"ScoringWeights must sum to 1.0, got {total:.3f} "
                f"(income={self.income}, business={self.business}, "
                f"dip={self.dip}, oversold={self.oversold})"
            )
        return self


class ScoreMultiplierCfg(BaseModel):
    """Position size multiplier bands (locked per architecture)."""
    high_threshold: float = Field(default=80.0, ge=50.0, le=100.0)
    mid_threshold: float = Field(default=60.0, ge=0.0, le=90.0)
    high: float = Field(default=1.2, ge=1.0, le=2.0)
    mid: float = Field(default=1.0, ge=0.5, le=1.5)
    low: float = Field(default=0.6, ge=0.0, le=1.0)
    annotation_max: float = Field(default=1.5, ge=1.0, le=3.0)


class SizingCfg(BaseModel):
    """Position sizing parameters."""
    base_allocation_pct: float = Field(default=0.05, ge=0.01, le=0.30)
    max_income_contribution: float = Field(default=0.15, ge=0.05, le=0.50)


class IncomeQualityCfg(BaseModel):
    """Income Quality scoring thresholds."""
    # FCF payout ratio — lower is safer — scores 0 if missing (no partial credit)
    payout_t1_max: float = Field(default=0.30); payout_t1_pts: float = Field(default=25.0)
    payout_t2_max: float = Field(default=0.50); payout_t2_pts: float = Field(default=20.0)
    payout_t3_max: float = Field(default=0.70); payout_t3_pts: float = Field(default=15.0)
    payout_t4_max: float = Field(default=0.90); payout_t4_pts: float = Field(default=8.0)
    # DPS CAGR — higher is better — scores 0 if missing
    growth_t1_min: float = Field(default=0.08); growth_t1_pts: float = Field(default=25.0)
    growth_t2_min: float = Field(default=0.05); growth_t2_pts: float = Field(default=22.0)
    growth_t3_min: float = Field(default=0.03); growth_t3_pts: float = Field(default=18.0)
    growth_t4_min: float = Field(default=0.00); growth_t4_pts: float = Field(default=12.0)
    growth_t5_min: float = Field(default=-0.05); growth_t5_pts: float = Field(default=5.0)


class BusinessQualityCfg(BaseModel):
    """Business Quality scoring thresholds."""
    # Revenue CAGR — higher is better — scores 0 if missing
    rev_t1_min: float = Field(default=0.12); rev_t1_pts: float = Field(default=25.0)
    rev_t2_min: float = Field(default=0.07); rev_t2_pts: float = Field(default=20.0)
    rev_t3_min: float = Field(default=0.03); rev_t3_pts: float = Field(default=15.0)
    rev_t4_min: float = Field(default=-0.02); rev_t4_pts: float = Field(default=8.0)
    rev_floor_pts: float = Field(default=2.0)
    # FCF margin — higher is better — scores 0 if missing
    margin_t1_min: float = Field(default=0.25); margin_t1_pts: float = Field(default=25.0)
    margin_t2_min: float = Field(default=0.15); margin_t2_pts: float = Field(default=20.0)
    margin_t3_min: float = Field(default=0.08); margin_t3_pts: float = Field(default=15.0)
    margin_t4_min: float = Field(default=0.03); margin_t4_pts: float = Field(default=10.0)
    margin_t5_min: float = Field(default=0.00); margin_t5_pts: float = Field(default=5.0)
    # FCF CAGR — higher is better — scores 0 if missing
    # FCF CAGR is more volatile than Revenue CAGR, so thresholds are lower.
    fcf_cagr_t1_min: float = Field(default=0.15); fcf_cagr_t1_pts: float = Field(default=25.0)
    fcf_cagr_t2_min: float = Field(default=0.10); fcf_cagr_t2_pts: float = Field(default=22.0)
    fcf_cagr_t3_min: float = Field(default=0.05); fcf_cagr_t3_pts: float = Field(default=18.0)
    fcf_cagr_t4_min: float = Field(default=0.00); fcf_cagr_t4_pts: float = Field(default=12.0)
    fcf_cagr_floor_pts: float = Field(default=2.0)
    # Net debt trend
    net_debt_stable_pct: float = Field(default=0.15)
    net_debt_improve_pts: float = Field(default=25.0)
    net_debt_stable_pts: float = Field(default=15.0)
    net_debt_worsen_pts: float = Field(default=5.0)


class OversoldQualityCfg(BaseModel):
    """Oversold Confidence scoring thresholds."""
    # RSI — lower is more oversold — scores 0 if missing (no partial credit)
    rsi_t1: float = Field(default=20.0); rsi_p1: float = Field(default=40.0)
    rsi_t2: float = Field(default=25.0); rsi_p2: float = Field(default=35.0)
    rsi_t3: float = Field(default=30.0); rsi_p3: float = Field(default=30.0)
    rsi_t4: float = Field(default=35.0); rsi_p4: float = Field(default=22.0)
    rsi_t5: float = Field(default=40.0); rsi_p5: float = Field(default=15.0)
    rsi_t6: float = Field(default=45.0); rsi_p6: float = Field(default=8.0)
    rsi_t7: float = Field(default=50.0); rsi_p7: float = Field(default=4.0)
    # Pct below 52W high — higher is more oversold
    pct_t1: float = Field(default=0.40); pct_p1: float = Field(default=35.0)
    pct_t2: float = Field(default=0.30); pct_p2: float = Field(default=30.0)
    pct_t3: float = Field(default=0.20); pct_p3: float = Field(default=22.0)
    pct_t4: float = Field(default=0.15); pct_p4: float = Field(default=16.0)
    pct_t5: float = Field(default=0.10); pct_p5: float = Field(default=10.0)
    pct_t6: float = Field(default=0.05); pct_p6: float = Field(default=5.0)
    # Volume ratio — higher is stronger selling — scores 0 if missing
    vol_t1: float = Field(default=3.0); vol_p1: float = Field(default=25.0)
    vol_t2: float = Field(default=2.0); vol_p2: float = Field(default=20.0)
    vol_t3: float = Field(default=1.5); vol_p3: float = Field(default=14.0)
    vol_t4: float = Field(default=1.2); vol_p4: float = Field(default=8.0)
    vol_t5: float = Field(default=0.8); vol_p5: float = Field(default=4.0)


class DipQualityCfg(BaseModel):
    """Dip Quality base scores and structural flag penalties."""
    base_transient: float = Field(default=87.5)
    base_transient_macro: float = Field(default=72.5)
    base_cyclical_macro: float = Field(default=62.5)
    base_cyclical_idio: float = Field(default=55.0)
    base_structural_exposed: float = Field(default=30.0)
    base_structural: float = Field(default=10.0)
    base_unknown: float = Field(default=50.0)
    flag_penalty: float = Field(default=5.0, ge=0.0)
    flag_penalty_max: float = Field(default=15.0, ge=0.0)


class MacroCfg(BaseModel):
    """Macro regime detection thresholds."""
    rate_shock_up_bps: int = Field(default=25, ge=5)
    rate_shock_down_bps: int = Field(default=-25, le=-5)
    nfci_tightening: float = Field(default=0.25)
    nfci_loose: float = Field(default=-0.25)
    recession_prob_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    growth_confidence: float = Field(default=0.75, ge=0.5, le=1.0)
    block_confidence: float = Field(default=0.70, ge=0.5, le=1.0)
    # States that block KIV→Candidate promotion when their axis confidence ≥ block_confidence
    block_rates_states: str = Field(default="RATE_SHOCK_UP",
                                    description="Comma-separated RatesState values that block promotion")
    block_fin_states: str = Field(default="TIGHTENING",
                                  description="Comma-separated FinancialConditions values that block promotion")

    def block_rates_set(self) -> set[str]:
        return {s.strip() for s in self.block_rates_states.split(",") if s.strip()}

    def block_fin_set(self) -> set[str]:
        return {s.strip() for s in self.block_fin_states.split(",") if s.strip()}


class MimoBatchCfg(BaseModel):
    """MiMo batch processing and caching settings."""
    batch_size: int = Field(default=5, ge=1, le=50,
        description="Number of stocks per single MiMo API call in batch mode")
    max_concurrent_batches: int = Field(default=2, ge=1, le=10,
        description="Max parallel MiMo API calls (higher = faster but may hit rate limits)")
    ttl_days: int = Field(default=90, ge=1, le=365,
        description="Days to cache MiMo classifications (reused until filing text changes)")
    max_chars_per_stock: int = Field(default=2000, ge=500, le=5000,
        description="Max characters per filing section (MD&A, risk factors) in batch prompt")


class FilingCfg(BaseModel):
    """EDGAR full-text filing extraction settings."""
    max_section_chars: int = Field(default=40_000, ge=1_000)
    rate_delay_seconds: float = Field(default=0.2, ge=0.05, le=5.0,
                                      description="Delay between EDGAR HTTP requests (5 req/s policy)")


class BacktestCfg(BaseModel):
    """Forward-return calibration defaults."""
    default_horizons: str = Field(default="30,90,180,365")
    score_bucket_boundaries: str = Field(default="0,60,70,80,100")
    # Gap B: minimum hit-rate a score bucket must achieve to be considered reliable.
    # 90-day horizon is the primary signal; 180-day is secondary confirmation.
    min_return_threshold_90d: float = Field(default=0.05, ge=0.0,
        description="Min total-return threshold to count as a 'hit' at 90d (default 5%)")
    min_hit_rate_90d: float = Field(default=0.55, ge=0.0, le=1.0,
        description="Min fraction of entries that must clear the 90d threshold to call a bucket reliable")
    min_return_threshold_180d: float = Field(default=0.08, ge=0.0,
        description="Min total-return threshold at 180d (default 8%)")
    min_hit_rate_180d: float = Field(default=0.50, ge=0.0, le=1.0,
        description="Min fraction of entries that must clear the 180d threshold")

    def horizons_list(self) -> list[int]:
        return [int(h.strip()) for h in self.default_horizons.split(",")]

    def bucket_boundaries_list(self) -> list[float]:
        return [float(b.strip()) for b in self.score_bucket_boundaries.split(",")]


# ═══════════════════════════════════════════════════════════════════════════
# Main Settings
# ═══════════════════════════════════════════════════════════════════════════

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # ── Connections ──────────────────────────────────────────────────────
    database_url: str = "postgresql://incomos:incomos@localhost:5432/incomos"
    mimo_api_key: str | None = None
    mimo_api_base_url: str = "https://api.mimo.ai/v1"
    mimo_model: str = "mimo-v2.5-pro"
    fred_api_key: str | None = None

    # ── Operational ──────────────────────────────────────────────────────
    base_currency: str = "MYR"
    edgar_requests_per_second: int = Field(default=5, ge=1, le=10)

    # ── Stage 0→1 quality screen ─────────────────────────────────────────
    min_dividend_years: int = Field(default=3, ge=1)
    max_fcf_payout_ratio: float = Field(default=0.90, gt=0, le=1.0)
    min_fcf_positive_years: int = Field(default=3, ge=1)
    # Gap G: regulated utilities (SIC 4900-4999) have structurally negative FCF
    # due to mandated capex recovery programs.  Use a relaxed FCF threshold for them.
    min_fcf_positive_years_utility: int = Field(default=1, ge=0,
        description="FCF positivity years required for regulated utilities (SIC 4900-4999)")
    utility_sic_min: int = Field(default=4900)
    utility_sic_max: int = Field(default=4999)

    # ── Staleness thresholds (hours) ─────────────────────────────────────
    staleness_macro_hours: float = 24.0
    staleness_kiv_price_hours: float = 24.0
    staleness_kiv_filings_hours: float = 24.0
    staleness_universe_hours: float = 168.0
    staleness_full_filing_hours: float = 2160.0

    # ── KIV TTL ──────────────────────────────────────────────────────────
    kiv_ttl_days: int = 90

    # ── Threshold sub-models ─────────────────────────────────────────────
    dip_trigger: DipTriggerCfg = Field(default_factory=DipTriggerCfg)
    entry_attractiveness: EntryAttractivenessCfg = Field(default_factory=EntryAttractivenessCfg)
    kiv: KivCfg = Field(default_factory=KivCfg)
    scoring_weights: ScoringWeightsCfg = Field(default_factory=ScoringWeightsCfg)
    score_multiplier: ScoreMultiplierCfg = Field(default_factory=ScoreMultiplierCfg)
    sizing: SizingCfg = Field(default_factory=SizingCfg)
    income_q: IncomeQualityCfg = Field(default_factory=IncomeQualityCfg)
    business_q: BusinessQualityCfg = Field(default_factory=BusinessQualityCfg)
    oversold_q: OversoldQualityCfg = Field(default_factory=OversoldQualityCfg)
    dip_q: DipQualityCfg = Field(default_factory=DipQualityCfg)
    macro: MacroCfg = Field(default_factory=MacroCfg)
    mimo_batch: MimoBatchCfg = Field(default_factory=MimoBatchCfg)
    filing: FilingCfg = Field(default_factory=FilingCfg)
    backtest: BacktestCfg = Field(default_factory=BacktestCfg)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
